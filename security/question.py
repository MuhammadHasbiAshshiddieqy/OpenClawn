import asyncio
import uuid
from dataclasses import dataclass, field

from infra.config import AppConfig
from infra.logging import log


@dataclass
class PendingQuestion:
    """Pertanyaan klarifikasi dari agent yang menunggu jawaban user dari Web UI."""

    question_id: str
    session_id: str
    question: str
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


class QuestionGate:
    """
    Klarifikasi human-in-the-loop untuk tool `ask_user`.

    Analog `ApprovalGate` tapi untuk pertanyaan terbuka (bukan ya/tidak):
    `ask()` membuat Future dan menunggu user mengetik jawaban di Web UI
    (via `resolve()`). Bila user tidak menjawab dalam `approval_timeout_sec`
    → kembalikan penanda "tidak dijawab" sehingga agent bisa melanjutkan
    dengan asumsi, bukan menggantung.

    Ephemeral by design: tidak ada tabel DB — jawaban klarifikasi tidak punya
    nilai audit seperti keputusan approval (CLAUDE.md §6, jangan tambah skema
    tanpa kebutuhan). State hanya Future in-memory + registry per session.
    """

    NO_ANSWER = "[user tidak menjawab dalam batas waktu]"

    def __init__(self, config: AppConfig):
        self.config = config
        self._pending: dict[str, PendingQuestion] = {}

    async def ask(self, session_id: str, question: str) -> str:
        """Ajukan pertanyaan & tunggu jawaban user. Timeout → NO_ANSWER (fail-soft)."""
        question_id = uuid.uuid4().hex
        pending = PendingQuestion(question_id=question_id, session_id=session_id, question=question)
        self._pending[question_id] = pending
        try:
            answer = await asyncio.wait_for(
                pending.future, timeout=self.config.approval_timeout_sec
            )
        except asyncio.TimeoutError:
            # Fail-soft: beda dari approval (yang fail-safe DENY). Pertanyaan tak
            # dijawab tidak berbahaya — biarkan agent lanjut dengan asumsi.
            log.warning("question_timeout", session=session_id, question_id=question_id)
            answer = self.NO_ANSWER
        finally:
            self._pending.pop(question_id, None)
        return answer

    def resolve(self, question_id: str, answer: str) -> bool:
        """Dipanggil dari Web UI saat user submit jawaban. Return True bila valid."""
        pending = self._pending.get(question_id)
        if pending and not pending.future.done():
            pending.future.set_result(answer)
            return True
        return False

    def resolve_by_session(self, session_id: str, answer: str) -> bool:
        """Resolve pertanyaan pending TERTUA untuk sebuah session.

        Frontend cukup mengirim session_id + jawaban tanpa melacak question_id:
        single-user dengan satu pertanyaan aktif per session pada satu waktu.
        Bila ada beberapa (jarang), jawab yang tertua dulu (FIFO).
        """
        for p in self._pending.values():
            if p.session_id == session_id and not p.future.done():
                p.future.set_result(answer)
                return True
        return False

    def pending_list(self, session_id: str | None = None) -> list[dict]:
        """Pertanyaan yang masih menunggu jawaban — untuk Web UI."""
        return [
            {
                "question_id": p.question_id,
                "session_id": p.session_id,
                "question": p.question,
            }
            for p in self._pending.values()
            if session_id is None or p.session_id == session_id
        ]
