import json
from pathlib import Path

from pydantic import BaseModel, ValidationError

from infra.database import DatabaseManager
from roles.contracts import CONTRACT_REGISTRY

_DEFAULT_ROLES_DIR = "roles"


def available_roles(roles_dir: str = _DEFAULT_ROLES_DIR) -> set[str]:
    """Nama role yang BENAR-BENAR ada (subdirektori `roles_dir` berisi `soul.toml`).

    Audit produksi 2026-09-18 — path traversal kritis: `role` yang datang dari
    luar (form field `/chat/stream`/`/converse/stream`, argumen tool
    `task_graph_submit`) SEBELUMNYA dipakai MENTAH untuk membangun path
    filesystem (`f"roles/{role}/soul.toml"`) di `core/agent_loop.py`,
    `core/router.py`, `core/late_execute.py` — TANPA validasi apa pun. String
    seperti `"../../../../tmp/evil"` memuat `soul.toml` ARBITRER dari mana pun
    di filesystem yang bisa dibaca proses (dikonfirmasi lewat reproduksi
    terisolasi: `AgentLoop` sukses memuat `[tools] allowed` bikinan sendiri
    dari luar `roles/`) — privilege escalation TOTAL, membypass seluruh model
    permission berbasis soul.toml/RBAC, dan bisa dipicu tanpa login sama
    sekali (auth default OFF).

    Fungsi ini SATU-SATUNYA validasi yang aman: `role in available_roles()`
    HARUS dicek SEBELUM `role` menyentuh path filesystem apa pun. Sekadar cek
    `Path(...).exists()` (pola lama `core/task_graph.py::TaskGraph.validate`)
    TIDAK CUKUP — exists() tetap True untuk path traversal yang benar-benar
    punya `soul.toml` di ujungnya, tidak mencegah traversal itu sendiri.
    `p.parent.name` selalu SATU komponen path (hasil glob satu-level, tak
    pernah mengandung `/` atau `..`), jadi keanggotaan set ini aman dipakai
    sebagai allow-list tertutup.
    """
    return {p.parent.name for p in Path(roles_dir).glob("*/soul.toml")}


def parse_contract(raw: str, contract_cls: type[BaseModel]) -> tuple[dict, bool]:
    """Parse teks mentah → instance contract Pydantic.

    Toleran terhadap pembungkus markdown ```json. Gagal → (dict berisi raw+error, False),
    tidak pernah crash. Dipakai ulang oleh RoleNegotiator dan ConversationOrchestrator.
    """
    try:
        cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
        instance = contract_cls(**json.loads(cleaned))
        return instance.model_dump(), True
    except (json.JSONDecodeError, ValidationError, TypeError) as e:
        return {"raw": raw[:500], "error": str(e)}, False


class RoleNegotiator:
    """
    Inovasi 4: handoff antar role tervalidasi dengan Pydantic contract.
    Output tidak valid → validation_ok=0, simpan raw untuk debugging. Jangan crash.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def handoff(
        self,
        session_id: str,
        from_role: str,
        to_role: str,
        task_input: str,
        agent_factory,
    ) -> dict:
        contract_cls = CONTRACT_REGISTRY.get(to_role)
        if not contract_cls:
            return {"error": f"Tidak ada contract untuk role '{to_role}'"}

        sub_agent = agent_factory(to_role)
        schema = json.dumps(contract_cls.model_json_schema(), indent=2)
        prompt = (
            f"{task_input}\n\nPENTING: Jawab dalam JSON sesuai schema, tanpa teks lain:\n{schema}"
        )

        # Fix: run() yield AgentEvent, bukan str — kumpulkan teks dari event type "token".
        raw = ""
        async for ev in sub_agent.run(prompt):
            if getattr(ev, "type", None) == "token":
                raw += ev.text

        validated, ok = parse_contract(raw, contract_cls)
        await self.db.execute(
            """INSERT INTO role_handoffs (session_id, from_role, to_role, task_input,
                                          contract_name, output_json, validation_ok)
               VALUES (?,?,?,?,?,?,?)""",
            (session_id, from_role, to_role, task_input, to_role, json.dumps(validated), int(ok)),
        )

        return {"from": from_role, "to": to_role, "output": validated, "valid": ok}

    def _validate(self, raw: str, contract_cls) -> tuple[dict, bool]:
        # Dipertahankan untuk kompatibilitas; delegasi ke helper modul-level.
        return parse_contract(raw, contract_cls)
