"""Sprint 4: Router self-calibration advisor (Inovasi 1, lanjutan).

calibration_report() menjawab "label mana yang sering dikoreksi".
Modul ini menjawab "apa yang harus diubah" — menghasilkan REKOMENDASI
threshold dari data audit, tanpa auto-apply. Mengubah angka router harus
keputusan manusia berdasarkan data nyata (CLAUDE.md §1.4, §8).

Murni & extractable: input berupa list[dict] dari calibration_report,
tidak menyentuh DB langsung. Bisa diekstrak jadi paket terpisah.
"""

from dataclasses import dataclass

# Ambang sampel minimum sebelum saran dianggap valid — hindari noise dari N kecil.
MIN_SAMPLE_FOR_SIGNAL = 10
# Correction-rate (%) di atas ini → label under-provisioned (router terlalu pelit).
HIGH_CORRECTION_RATE = 20.0
# Correction-rate (%) di bawah ini PADA label cloud berbiaya → kandidat over-provisioned.
LOW_CORRECTION_RATE = 5.0

# Label yang memakai Claude (berbiaya). Over-provisioning di sini = buang uang.
CLOUD_LABELS = {"complex", "critical"}
# Urutan kompleksitas untuk menyarankan arah perubahan.
COMPLEXITY_ORDER = ["trivial", "simple", "moderate", "complex", "critical"]


@dataclass
class Recommendation:
    """Satu saran tuning untuk satu complexity label."""

    label: str
    issue: str  # "under_provisioned" | "over_provisioned"
    correction_rate: float
    sample_size: int
    suggestion: str


class RoutingCalibrator:
    """Menerjemahkan calibration_report → rekomendasi threshold yang bisa dibaca manusia."""

    def __init__(
        self,
        min_sample: int = MIN_SAMPLE_FOR_SIGNAL,
        high_rate: float = HIGH_CORRECTION_RATE,
        low_rate: float = LOW_CORRECTION_RATE,
    ):
        self.min_sample = min_sample
        self.high_rate = high_rate
        self.low_rate = low_rate

    def analyze(self, report: list[dict]) -> list[Recommendation]:
        """
        Hasilkan rekomendasi dari calibration_report.

        report: list dict dengan keys complexity_label, total, corrections,
                correction_rate, avg_cost (lihat RoutingAuditor.calibration_report).
        """
        recs: list[Recommendation] = []
        for row in report:
            label = row.get("complexity_label", "")
            total = row.get("total", 0) or 0
            rate = row.get("correction_rate") or 0.0
            avg_cost = row.get("avg_cost") or 0.0

            # Sampel terlalu kecil → jangan beri sinyal, bisa menyesatkan.
            if total < self.min_sample:
                continue

            # Under-provisioned: sering dikoreksi → naikkan kompleksitas lebih cepat.
            if rate >= self.high_rate:
                recs.append(
                    Recommendation(
                        label=label,
                        issue="under_provisioned",
                        correction_rate=rate,
                        sample_size=total,
                        suggestion=self._suggest_upgrade(label),
                    )
                )
            # Over-provisioned: label cloud berbiaya tapi nyaris tak pernah dikoreksi
            # → mungkin query ini bisa ditangani tier lebih murah.
            elif label in CLOUD_LABELS and rate <= self.low_rate and avg_cost > 0:
                recs.append(
                    Recommendation(
                        label=label,
                        issue="over_provisioned",
                        correction_rate=rate,
                        sample_size=total,
                        suggestion=self._suggest_downgrade(label),
                    )
                )
        return recs

    def _suggest_upgrade(self, label: str) -> str:
        nxt = self._neighbor(label, direction=+1)
        if nxt:
            return (
                f"Label '{label}' sering dikoreksi. Pertimbangkan menurunkan threshold "
                f"agar query serupa naik ke '{nxt}' lebih cepat, atau tambah upgrade_keyword "
                f"di soul.toml role terkait."
            )
        return f"Label '{label}' sudah tier tertinggi; tinjau kualitas prompt/skill, bukan routing."

    def _suggest_downgrade(self, label: str) -> str:
        prev = self._neighbor(label, direction=-1)
        if prev:
            return (
                f"Label '{label}' (cloud, berbiaya) nyaris tak pernah dikoreksi. "
                f"Pertimbangkan menaikkan threshold agar sebagian query turun ke '{prev}' "
                f"untuk hemat biaya tanpa mengorbankan kualitas."
            )
        return f"Label '{label}' sudah tier terendah cloud; tinjau manual."

    def _neighbor(self, label: str, direction: int) -> str | None:
        if label not in COMPLEXITY_ORDER:
            return None
        idx = COMPLEXITY_ORDER.index(label) + direction
        if 0 <= idx < len(COMPLEXITY_ORDER):
            return COMPLEXITY_ORDER[idx]
        return None

    def summary(self, report: list[dict]) -> dict:
        """Ringkasan siap-tampil untuk /metrics: jumlah saran + daftar rekomendasi."""
        recs = self.analyze(report)
        total_events = sum((r.get("total", 0) or 0) for r in report)
        return {
            "total_events": total_events,
            "has_enough_data": total_events >= self.min_sample,
            "recommendations": [
                {
                    "label": r.label,
                    "issue": r.issue,
                    "correction_rate": r.correction_rate,
                    "sample_size": r.sample_size,
                    "suggestion": r.suggestion,
                }
                for r in recs
            ],
        }
