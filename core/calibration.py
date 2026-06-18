"""Router self-calibration (Inovasi 1, lanjutan) — advisor + loop tertutup.

Dua bagian, sengaja dipisah:

1. `RoutingCalibrator` — MURNI. calibration_report() menjawab "label mana yang
   sering dikoreksi"; kelas ini menjawab "apa yang harus diubah" → REKOMENDASI
   threshold dari list[dict], tanpa menyentuh DB. Extractable jadi paket terpisah.

2. `CalibrationStore` — DB-bound (hanya bergantung DatabaseManager, §1.6). Menutup
   loop: menyimpan offset threshold aktif ke app_settings (dibaca SmartRouter) +
   jejak audit setiap perubahan ke calibration_log, sehingga apply bisa di-REVERT.
   Apply tetap keputusan manusia (tombol di /metrics), bukan auto-apply (§8).
"""

from dataclasses import dataclass

from infra.database import DatabaseManager

# Key di app_settings tempat offset threshold aktif disimpan (dibaca SmartRouter).
ROUTER_OFFSET_KEY = "router_threshold_offset"
# Batasi offset agar kalibrasi tak pernah membuat router mustahil (semua trivial/critical).
OFFSET_MIN, OFFSET_MAX = -3, 3

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
    # Arah geser offset yang disarankan untuk diterapkan ke CalibrationStore:
    # -1 (under → naik tier lebih cepat) | +1 (over → bertahan murah lebih lama).
    offset_delta: int = 0


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
                        offset_delta=-1,  # turunkan threshold → naik tier lebih cepat
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
                        offset_delta=+1,  # naikkan threshold → bertahan tier murah lebih lama
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
        """Ringkasan siap-tampil untuk /metrics: jumlah saran + daftar rekomendasi.

        `net_offset_delta` = arah geser global yang disarankan (jumlah delta, dijepit
        ke satu langkah {-1,0,+1}). Frontend memakainya untuk tombol Apply satu-klik.
        """
        recs = self.analyze(report)
        total_events = sum((r.get("total", 0) or 0) for r in report)
        net = sum(r.offset_delta for r in recs)
        net_clamped = max(-1, min(1, net))  # satu apply = satu langkah; hindari lompatan
        return {
            "total_events": total_events,
            "has_enough_data": total_events >= self.min_sample,
            "net_offset_delta": net_clamped,
            "recommendations": [
                {
                    "label": r.label,
                    "issue": r.issue,
                    "correction_rate": r.correction_rate,
                    "sample_size": r.sample_size,
                    "suggestion": r.suggestion,
                    "offset_delta": r.offset_delta,
                }
                for r in recs
            ],
        }


class CalibrationStore:
    """Loop tertutup Inovasi 1: kelola offset threshold aktif + jejak audit.

    Hanya bergantung DatabaseManager (§1.6). Offset aktif disimpan di app_settings
    (key `router_threshold_offset`) — dibaca SmartRouter saat dibuat. Setiap perubahan
    dicatat ke calibration_log agar bisa di-revert dan diaudit.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_offset(self) -> int:
        """Offset threshold aktif. Default 0 (router asli) bila belum pernah diset."""
        row = await self.db.fetchone(
            "SELECT value FROM app_settings WHERE key=?", (ROUTER_OFFSET_KEY,)
        )
        if not row or row["value"] is None:
            return 0
        try:
            return int(row["value"])
        except (ValueError, TypeError):
            return 0  # fail-safe: nilai korup → anggap netral, jangan crash router

    async def _set_offset(self, value: int) -> None:
        await self.db.execute(
            """INSERT INTO app_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=CURRENT_TIMESTAMP""",
            (ROUTER_OFFSET_KEY, str(value)),
        )

    async def apply(self, delta: int, reason: str, source: str = "calibration") -> dict:
        """Geser offset sebesar `delta`, catat ke audit log. Return state baru.

        Dijepit ke [OFFSET_MIN, OFFSET_MAX] agar kalibrasi tak pernah ekstrem.
        Bila offset tak berubah (sudah di batas) → tetap catat sebagai no-op informatif.
        """
        old = await self.get_offset()
        new = max(OFFSET_MIN, min(OFFSET_MAX, old + delta))
        # Baris audit lama bukan lagi state aktif terakhir.
        await self.db.execute("UPDATE calibration_log SET active=0 WHERE active=1")
        await self.db.execute(
            """INSERT INTO calibration_log (old_offset, new_offset, reason, source, active)
               VALUES (?, ?, ?, ?, 1)""",
            (old, new, reason, source),
        )
        await self._set_offset(new)
        return {"old_offset": old, "new_offset": new, "changed": old != new}

    async def revert(self) -> dict:
        """Kembalikan offset ke state SEBELUM apply aktif terakhir.

        Memakai `old_offset` dari baris aktif sebagai target revert, lalu menandai
        baris itu non-aktif & mencatat baris revert baru (audit tetap utuh).
        Bila tak ada riwayat → no-op (offset tetap 0).
        """
        current = await self.db.fetchone(
            "SELECT id, old_offset, new_offset FROM calibration_log WHERE active=1"
        )
        if not current:
            return {"reverted": False, "reason": "tidak ada kalibrasi aktif untuk di-revert"}
        target = current["old_offset"]
        await self.db.execute("UPDATE calibration_log SET active=0 WHERE active=1")
        await self.db.execute(
            """INSERT INTO calibration_log (old_offset, new_offset, reason, source, active)
               VALUES (?, ?, ?, 'revert', 1)""",
            (current["new_offset"], target, "revert kalibrasi sebelumnya"),
        )
        await self._set_offset(target)
        return {"reverted": True, "old_offset": current["new_offset"], "new_offset": target}

    async def history(self, limit: int = 20) -> list[dict]:
        """Riwayat perubahan offset terbaru-dulu, untuk ditampilkan di /metrics."""
        return await self.db.fetchall(
            """SELECT old_offset, new_offset, reason, source, active, created_at
               FROM calibration_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
