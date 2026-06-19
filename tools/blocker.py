"""Tool report_blocker: agent menandai hambatan secara terstruktur.

Terinspirasi "proactive blocker reporting" Multica. Beda dari `ask_user` (yang
MEMBLOKIR menunggu jawaban manusia): report_blocker bersifat ASINKRON — agent
melaporkan hambatan lalu boleh lanjut/berhenti, user meninjau kapan saja di UI.

Gunakan saat agent menemukan penghalang yang tak bisa diselesaikan sendiri tapi
tidak ingin menggantung menunggu (mis. kredensial hilang, kebutuhan ambigu,
dependency eksternal mati). Read-write ke tabel internal → tidak butuh approval.

session_id & role disuntik AgentLoop (`_session_id`, `_role`) — model tak mengarang.
"""

from tools.base import Tool

VALID_SEVERITY = {"low", "medium", "high"}


class ReportBlockerTool(Tool):
    name = "report_blocker"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        session_id = input_data.get("_session_id")
        role = input_data.get("_role") or "unknown"
        if not session_id:
            return {"error": "report_blocker butuh konteks sesi (internal)"}
        if db is None:
            return {"error": "report_blocker butuh database"}
        summary = str(input_data.get("summary", "")).strip()
        if not summary:
            return {"error": "summary wajib: jelaskan singkat apa yang menghambat"}
        detail = str(input_data.get("detail", "")).strip()
        severity = str(input_data.get("severity", "medium")).strip().lower()
        if severity not in VALID_SEVERITY:
            return {"error": f"severity '{severity}' tak valid (low/medium/high)"}

        await db.execute(
            """INSERT INTO agent_blockers (session_id, role, summary, detail, severity)
               VALUES (?,?,?,?,?)""",
            (session_id, role, summary[:500], detail[:2000], severity),
        )
        return {
            "ok": True,
            "reported": summary[:500],
            "severity": severity,
            "note": "Hambatan tercatat & ditampilkan ke user. Lanjutkan jika bisa, atau hentikan dengan menjelaskan keadaan.",
        }

    def schema(self) -> dict:
        return {
            "name": "report_blocker",
            "description": (
                "Laporkan hambatan yang menghalangi penyelesaian tugas (mis. kredensial "
                "hilang, kebutuhan ambigu, dependency mati). ASINKRON — tidak menunggu "
                "jawaban (beda dari ask_user). Pakai saat butuh perhatian user tapi tak "
                "ingin menggantung. Sertakan summary singkat + severity."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Ringkas: apa yang menghambat."},
                    "detail": {"type": "string", "description": "Konteks tambahan (opsional)."},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Tingkat dampak (default medium).",
                    },
                },
                "required": ["summary"],
            },
        }
