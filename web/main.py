import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.agent_loop import AgentConfig, AgentLoop
from core.audit import RoutingAuditor
from core.calibration import RoutingCalibrator
from infra.config import CONFIG
from infra.database import DatabaseManager
from infra.logging import setup_logging
from security.approval import ApprovalGate

db = DatabaseManager(CONFIG)
# ApprovalGate shared di tingkat app: AgentLoop dibuat baru tiap request, tapi
# Future approval harus bertahan agar /approve bisa me-resolve-nya (HITL §17).
approval_gate = ApprovalGate(db, CONFIG)


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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "role": role,
            "available_roles": ["pm", "qa", "dev"],
            "session_id": str(uuid.uuid4()),
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
        yield "data: <div class='msg assistant'>\n\n"
        async for token in agent.run(message):
            safe = token.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
            yield f"data: {safe}\n\n"
        yield "data: </div>\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
