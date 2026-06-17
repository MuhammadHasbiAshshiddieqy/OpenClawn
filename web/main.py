import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.agent_loop import AgentConfig, AgentLoop
from core.audit import RoutingAuditor
from core.calibration import RoutingCalibrator
from core.conversation import (
    ConversationControl,
    ConversationOrchestrator,
    make_strategy,
)
from infra.config import CONFIG
from infra.database import DatabaseManager
from infra.logging import log, setup_logging
from infra.settings import KNOWN_MODELS, SettingsStore
from security.approval import ApprovalGate

db = DatabaseManager(CONFIG)
# ApprovalGate shared di tingkat app: AgentLoop dibuat baru tiap request, tapi
# Future approval harus bertahan agar /approve bisa me-resolve-nya (HITL §17).
approval_gate = ApprovalGate(db, CONFIG)

# Registry kontrol percakapan per session — agar /converse/interject & /stop bisa
# mencapai loop yang sedang berjalan di /converse/stream (pola sama ApprovalGate._pending).
_conversations: dict[str, ConversationControl] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await db.run_migration("migrations/001_initial.sql")
    yield
    await db.close()


app = FastAPI(title="OpenCLAWN", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="web/static"), name="static")
templates = Jinja2Templates(directory="web/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, role: str = "pm"):
    # Tampilkan model aktif di sidebar: override eksplisit, atau "Auto (router)".
    override = await SettingsStore(db).get_model_override()
    active_model = f"{override[0]} / {override[1]}" if override else None
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "role": role,
            "available_roles": ["pm", "qa", "dev"],
            "session_id": str(uuid.uuid4()),
            "active_model": active_model,
        },
    )


@app.post("/chat/stream")
async def chat_stream(request: Request):
    form = await request.form()
    message = (form.get("message") or "").strip()
    role = form.get("role", "pm")
    session_id = form.get("session_id", str(uuid.uuid4()))
    if not message:
        return HTMLResponse("")

    agent = AgentLoop(AgentConfig(role=role, session_id=session_id), db=db, approval=approval_gate)

    async def generate():
        # Protokol SSE bernama: frontend membedakan `token` (isi jawaban) dari
        # `status` (proses berjalan: routing/thinking/tool/fallback) agar user
        # tahu agent sedang apa. `error`/`done` menandai akhir stream.
        try:
            async for event in agent.run(message):
                if event.type == "token":
                    # Kirim teks MENTAH (JSON-encoded agar newline tidak memecah frame
                    # SSE). Frontend yang merender markdown → HTML + sanitasi. Jangan
                    # escape/<br> di sini, supaya markdown (heading/list/code) utuh.
                    yield f"event: token\ndata: {json.dumps(event.text)}\n\n"
                elif event.type == "status":
                    payload = json.dumps({"text": event.text, "detail": event.detail})
                    yield f"event: status\ndata: {payload}\n\n"
        except Exception as exc:  # noqa: BLE001 — laporkan ke UI, jangan diam saat macet
            log.error("chat_stream_failed", session=session_id, error=str(exc))
            msg = json.dumps({"text": str(exc)})
            yield f"event: error\ndata: {msg}\n\n"
        finally:
            yield "event: done\ndata: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/converse/stream")
async def converse_stream(request: Request):
    """Multi-agent conversation: beberapa role saling mengobrol, di-stream per giliran."""
    form = await request.form()
    message = (form.get("message") or "").strip()
    pattern = (form.get("pattern") or "pipeline").strip()
    session_id = form.get("session_id", str(uuid.uuid4()))
    rounds = int(form.get("rounds") or 0)
    participants_raw = (form.get("participants") or "").strip()
    participants = [p.strip() for p in participants_raw.split(",") if p.strip()] or None
    if not message:
        return HTMLResponse("")

    try:
        strategy = make_strategy(pattern, participants, rounds, CONFIG)
    except ValueError as e:
        return HTMLResponse(f"event: error\ndata: {json.dumps({'text': str(e)})}\n\n")

    # STOP: implicit lewat disconnect; INTERJECT lewat registry per session.
    control = ConversationControl(disconnect_check=request.is_disconnected)
    _conversations[session_id] = control

    def agent_factory(role: str) -> AgentLoop:
        return AgentLoop(
            AgentConfig(role=role, session_id=session_id), db=db, approval=approval_gate
        )

    orch = ConversationOrchestrator(
        strategy=strategy,
        db=db,
        agent_factory=agent_factory,
        session_id=session_id,
        config=CONFIG,
        control=control,
    )

    async def generate():
        try:
            async for ev in orch.run(message):
                if ev.type == "turn":
                    payload = json.dumps({"role": ev.role, "label": ev.text, "turn": ev.turn_index})
                    yield f"event: turn\ndata: {payload}\n\n"
                elif ev.type == "token":
                    payload = json.dumps({"role": ev.role, "text": ev.text})
                    yield f"event: token\ndata: {payload}\n\n"
                elif ev.type == "status":
                    payload = json.dumps({"role": ev.role, "text": ev.text, "detail": ev.detail})
                    yield f"event: status\ndata: {payload}\n\n"
                elif ev.type == "conversation_end":
                    yield f"event: conversation_end\ndata: {json.dumps({'reason': ev.detail})}\n\n"
        except Exception as exc:  # noqa: BLE001 — laporkan ke UI, jangan diam
            log.error("converse_stream_failed", session=session_id, error=str(exc))
            yield f"event: error\ndata: {json.dumps({'text': str(exc)})}\n\n"
        finally:
            _conversations.pop(session_id, None)
            yield "event: done\ndata: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/converse/interject")
async def converse_interject(request: Request):
    """User menyela percakapan yang sedang berjalan; disuntik ke giliran berikutnya."""
    form = await request.form()
    session_id = (form.get("session_id") or "").strip()
    message = (form.get("message") or "").strip()
    control = _conversations.get(session_id)
    if not control or not message:
        return {"ok": False, "error": "sesi tidak aktif atau pesan kosong"}
    control.add_interjection(message)
    return {"ok": True}


@app.post("/converse/stop")
async def converse_stop(request: Request):
    """Hentikan percakapan (cadangan; STOP utama lewat AbortController di frontend)."""
    form = await request.form()
    session_id = (form.get("session_id") or "").strip()
    control = _conversations.get(session_id)
    if not control:
        return {"ok": False, "error": "sesi tidak aktif"}
    control.stop()
    return {"ok": True}


@app.get("/approvals")
async def approvals(session_id: str | None = None):
    """Daftar approval yang menunggu keputusan — dipakai Web UI untuk polling HITL."""
    return {"pending": approval_gate.pending_list(session_id)}


@app.post("/approve")
async def approve(request: Request):
    """User menekan approve/reject di Web UI → resolve Future approval."""
    form = await request.form()
    approval_id = (form.get("approval_id") or "").strip()
    decision = (form.get("decision") or "").strip().lower()
    if not approval_id or decision not in ("approve", "reject"):
        return {"ok": False, "error": "approval_id dan decision (approve|reject) wajib"}

    resolved = approval_gate.resolve(approval_id, decision == "approve")
    return {"ok": resolved, "approval_id": approval_id, "decision": decision}


@app.get("/metrics", response_class=HTMLResponse)
async def metrics(request: Request):
    report = await RoutingAuditor(db).calibration_report()
    # Sprint 4: tampilkan rekomendasi tuning (saran saja, tidak auto-apply ke router)
    calibration = RoutingCalibrator().summary(report)
    return templates.TemplateResponse(
        request,
        "metrics.html",
        {"report": report, "calibration": calibration},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: bool = False):
    """Halaman override model. Override = pilihan sadar; kosong = router otomatis."""
    store = SettingsStore(db)
    current = await store.get_model_override()  # (provider, model) | None
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "known_models": KNOWN_MODELS,
            "current": current,  # None artinya mode otomatis (router)
            "saved": saved,
        },
    )


@app.post("/settings")
async def settings_save(request: Request):
    """Simpan override. Nilai 'auto' (atau kosong) → hapus override, kembali ke router."""
    form = await request.form()
    choice = (form.get("model_choice") or "").strip()
    store = SettingsStore(db)

    if not choice or choice == "auto":
        await store.set_model_override(None, None)
    else:
        # value dropdown berformat "provider|model"
        provider, _, model = choice.partition("|")
        if provider and model:
            await store.set_model_override(provider, model)

    return RedirectResponse(url="/settings?saved=true", status_code=303)
