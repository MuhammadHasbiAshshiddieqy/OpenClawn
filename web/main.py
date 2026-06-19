import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.activity import ActivityTimeline
from core.agent_loop import AgentConfig, AgentLoop
from core.audit import RoutingAuditor
from core.autopilot import AutopilotScheduler, AutopilotStore
from core.calibration import CalibrationStore, RoutingCalibrator
from core.router import Complexity, SmartRouter
from core.router_config import RouterConfigStore
from core.tool_audit import ToolAudit
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
from security.question import QuestionGate

db = DatabaseManager(CONFIG)
# ApprovalGate & QuestionGate shared di tingkat app: AgentLoop dibuat baru tiap
# request, tapi Future-nya harus bertahan agar /approve & /answer bisa me-resolve
# Future yang sama (HITL §17 untuk approval; klarifikasi interaktif untuk ask_user).
approval_gate = ApprovalGate(db, CONFIG)
question_gate = QuestionGate(CONFIG)

# Registry kontrol percakapan per session — agar /converse/interject & /stop bisa
# mencapai loop yang sedang berjalan di /converse/stream (pola sama ApprovalGate._pending).
_conversations: dict[str, ConversationControl] = {}


async def _run_autopilot(ap: dict) -> int:
    """Jalankan satu autopilot: AgentLoop mode autopilot (read-only + antrian proposal).

    Keamanan (§1, §17): autopilot=True → tool butuh-approval TIDAK dieksekusi, diantri
    sebagai proposal. Di sini kita HITUNG berapa proposal yang masuk selama run agar
    user tahu ada aksi menunggu ditinjau. Return jumlah proposal baru.
    """
    session_id = f"autopilot-{ap['id']}"

    async def _count_proposals() -> int:
        row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM approval_log WHERE session_id=? AND decision='proposal:pending'",
            (session_id,),
        )
        return (row or {}).get("n", 0)

    before = await _count_proposals()
    agent = AgentLoop(
        AgentConfig(role=ap["role"], session_id=session_id, autopilot=True),
        db=db,
        approval=approval_gate,
        question_gate=question_gate,
    )
    # Drain stream sampai selesai; output tidak di-stream ke mana pun (tak ada user live).
    async for _ev in agent.run(ap["prompt"]):
        pass
    after = await _count_proposals()
    return max(0, after - before)


autopilot_store = AutopilotStore(db)
autopilot_scheduler = AutopilotScheduler(autopilot_store, runner=_run_autopilot, config=CONFIG)

# Urutan tampil role yang sudah dikenal; role lain (folder soul.toml baru) muncul
# setelahnya secara alfabetis. Daftar role di-scan dari folder roles/ agar menambah
# role = cukup membuat folder soul.toml, tanpa menyentuh web layer.
_ROLE_ORDER = ["pm", "dev", "qa", "data", "security"]
# Metadata tampilan: [judul, deskripsi singkat] untuk topbar & sidebar.
ROLES_META = {
    "pm": ["Product", "breakdown & prioritas"],
    "qa": ["Quality", "review & test"],
    "dev": ["Developer", "implement & fix"],
    "data": ["Data", "analisis, statistik & insight"],
    "security": ["Security & Privacy", "audit keamanan & privasi (read-only)"],
}


def available_roles() -> list[str]:
    """Daftar role dari folder roles/ yang punya soul.toml, urutan stabil."""
    roles_dir = Path("roles")
    found = {p.parent.name for p in roles_dir.glob("*/soul.toml")}
    known = [r for r in _ROLE_ORDER if r in found]
    extra = sorted(found - set(known))
    return known + extra


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await db.run_migration("migrations/001_initial.sql")
    # Scheduler autopilot hidup selama server hidup (loop asyncio in-process).
    autopilot_scheduler.start()
    yield
    await autopilot_scheduler.stop()
    await db.close()


app = FastAPI(title="OpenCLAWN", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="web/static"), name="static")
templates = Jinja2Templates(directory="web/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, role: str = "pm"):
    # Tampilkan model aktif di sidebar: override eksplisit, atau "Auto (router)".
    override = await SettingsStore(db).get_model_override()
    active_model = f"{override[0]} / {override[1]}" if override else None
    roles = available_roles()
    # Fallback aman bila ?role= menunjuk role yang tak ada.
    if role not in roles:
        role = roles[0] if roles else "pm"
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "role": role,
            "available_roles": roles,
            "roles_meta": ROLES_META,
            "default_participants": list(CONFIG.conversation_default_participants),
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

    agent = AgentLoop(
        AgentConfig(role=role, session_id=session_id),
        db=db,
        approval=approval_gate,
        question_gate=question_gate,
    )

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
                elif event.type == "thinking":
                    yield f"event: thinking\ndata: {json.dumps(event.text)}\n\n"
                elif event.type == "status":
                    payload = json.dumps({"text": event.text, "detail": event.detail})
                    yield f"event: status\ndata: {payload}\n\n"
                elif event.type == "usage":
                    # Ringkasan turn termasuk meter budget token (context vs max, §1.4).
                    yield f"event: usage\ndata: {json.dumps(event.usage)}\n\n"
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
            AgentConfig(role=role, session_id=session_id),
            db=db,
            approval=approval_gate,
            question_gate=question_gate,
        )

    orch = ConversationOrchestrator(
        strategy=strategy,
        db=db,
        agent_factory=agent_factory,
        session_id=session_id,
        config=CONFIG,
        control=control,
        pattern=pattern,
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
                elif ev.type == "thinking":
                    payload = json.dumps({"role": ev.role, "text": ev.text})
                    yield f"event: thinking\ndata: {payload}\n\n"
                elif ev.type == "status":
                    payload = json.dumps({"role": ev.role, "text": ev.text, "detail": ev.detail})
                    yield f"event: status\ndata: {payload}\n\n"
                elif ev.type == "conversation_end":
                    end = {"reason": ev.detail, "usage": ev.usage}
                    yield f"event: conversation_end\ndata: {json.dumps(end)}\n\n"
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


@app.post("/answer")
async def answer(request: Request):
    """User menjawab pertanyaan klarifikasi (ask_user) → resolve Future QuestionGate."""
    form = await request.form()
    session_id = (form.get("session_id") or "").strip()
    text = (form.get("answer") or "").strip()
    if not session_id or not text:
        return {"ok": False, "error": "session_id dan answer wajib"}
    resolved = question_gate.resolve_by_session(session_id, text)
    return {"ok": resolved}


@app.get("/metrics", response_class=HTMLResponse)
async def metrics(request: Request):
    report = await RoutingAuditor(db).calibration_report()
    # Rekomendasi tuning (saran); apply tetap keputusan manusia via tombol di bawah.
    calibration = RoutingCalibrator().summary(report)
    store = CalibrationStore(db)
    calibration["current_offset"] = await store.get_offset()
    calibration["history"] = await store.history()
    tool_stats = await ToolAudit(db).summary()
    return templates.TemplateResponse(
        request,
        "metrics.html",
        {"report": report, "calibration": calibration, "tool_stats": tool_stats},
    )


@app.post("/calibration/apply")
async def calibration_apply(request: Request):
    """Terapkan rekomendasi kalibrasi: geser offset threshold router + catat audit.

    Loop tertutup #1: ini satu-satunya jalur yang mengubah perilaku router dari data.
    Tetap dipicu manusia (bukan auto-apply, §8). delta dibatasi {-1,0,+1} per klik.
    """
    form = await request.form()
    try:
        delta = int(form.get("delta") or 0)
    except (ValueError, TypeError):
        delta = 0
    delta = max(-1, min(1, delta))  # satu langkah per apply
    if delta == 0:
        return RedirectResponse(url="/metrics", status_code=303)
    reason = (form.get("reason") or "kalibrasi dari /metrics").strip()
    result = await CalibrationStore(db).apply(delta, reason, source="calibration")
    log.info("calibration_applied", **result, reason=reason)
    return RedirectResponse(url="/metrics", status_code=303)


@app.post("/calibration/revert")
async def calibration_revert(request: Request):
    """Batalkan kalibrasi aktif terakhir, kembalikan offset ke state sebelumnya."""
    result = await CalibrationStore(db).revert()
    log.info("calibration_reverted", **result)
    return RedirectResponse(url="/metrics", status_code=303)


@app.get("/skills", response_class=HTMLResponse)
async def skills_page(request: Request):
    """Visualisasi Inovasi 2: skill active/draft/archived + skor decay terproyeksi.

    decay_score di DB hanya diperbarui saat decay pass (throttle 1 jam), jadi bisa
    sedikit stale. Untuk tampilan kita HITUNG skor terproyeksi (read-only) dengan
    formula yang sama (base ^ hari sejak dipakai) agar kurva mencerminkan keadaan kini.
    """
    rows = await db.fetchall(
        """SELECT role, skill_name, status, confidence, generator_model,
                  use_count, last_used_at, decay_score, created_at,
                  julianday('now') - julianday(COALESCE(last_used_at, created_at)) AS days_idle
           FROM skills
           ORDER BY status='archived', role, decay_score DESC, skill_name"""
    )
    base = CONFIG.skill_decay_base
    threshold = CONFIG.skill_archive_threshold
    skills = []
    counts = {"active": 0, "draft": 0, "archived": 0}
    for r in rows:
        days = max(0.0, r["days_idle"] or 0.0)
        # Proyeksi skor saat ini; arsip tetap pakai skor tersimpan (sudah final).
        if r["status"] == "active":
            projected = (r["decay_score"] or 0.0) * (base**days)
        else:
            projected = r["decay_score"] or 0.0
        projected = max(0.0, min(1.0, projected))
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        skills.append(
            {
                **r,
                "days_idle": round(days, 1),
                "projected_score": round(projected, 3),
                "score_pct": round(projected * 100),
                "near_archive": r["status"] == "active" and projected < threshold * 1.5,
            }
        )
    # Inovasi 3 observability: percobaan kristalisasi terakhir (keputusan evaluator).
    crystallization = await db.fetchall(
        """SELECT role, skill_name, generator_model, evaluator_model,
                  confidence, critical_gaps, status, reasoning, created_at
           FROM crystallization_log ORDER BY id DESC LIMIT 20"""
    )
    return templates.TemplateResponse(
        request,
        "skills.html",
        {
            "skills": skills,
            "counts": counts,
            "threshold": threshold,
            "threshold_pct": round(threshold * 100),
            "decay_base": base,
            "crystallization": crystallization,
            "confidence_threshold": CONFIG.confidence_threshold,
        },
    )


@app.get("/conversations", response_class=HTMLResponse)
async def conversations_page(request: Request):
    """Arsip percakapan multi-agent (pipeline/debate/orchestrator) untuk ditinjau ulang."""
    rows = await db.fetchall(
        """SELECT id, pattern, participants, initial_message, transcript_json,
                  turns, end_reason, cost_usd, created_at
           FROM conversations ORDER BY id DESC LIMIT 50"""
    )
    convos = []
    for r in rows:
        try:
            transcript = json.loads(r["transcript_json"])
        except (json.JSONDecodeError, TypeError):
            transcript = []
        convos.append({**r, "transcript": transcript})
    return templates.TemplateResponse(
        request,
        "conversations.html",
        {"conversations": convos},
    )


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request, role: str | None = None):
    """Linimasa kronologis aksi agent (terinspirasi Activity Timeline Multica).

    Agregasi read-only lintas tabel — tidak menulis apa pun. Filter `?role=` opsional
    memfokuskan pada satu peran (padanan 'agent profile').
    """
    roles = available_roles()
    # Validasi filter: role tak dikenal → abaikan (tampilkan semua), jangan error.
    active_role = role if role in roles else None
    timeline = await ActivityTimeline(db).recent(role=active_role)
    # Blocker terbuka ditampilkan menonjol di atas linimasa (proactive reporting).
    open_blockers = await db.fetchall(
        """SELECT id, role, summary, detail, severity, created_at
           FROM agent_blockers WHERE status='open'
           ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                    id DESC LIMIT 20"""
    )
    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            "events": timeline,
            "kinds": ActivityTimeline.KINDS,
            "roles": roles,
            "active_role": active_role,
            "open_blockers": open_blockers,
        },
    )


@app.post("/blockers/resolve")
async def blockers_resolve(request: Request):
    """Tandai blocker sebagai resolved (user sudah menanggapi)."""
    form = await request.form()
    try:
        blocker_id = int(form.get("blocker_id") or 0)
    except (ValueError, TypeError):
        blocker_id = 0
    if blocker_id:
        await db.execute(
            "UPDATE agent_blockers SET status='resolved', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            (blocker_id,),
        )
    return RedirectResponse(url="/activity", status_code=303)


@app.get("/autopilots", response_class=HTMLResponse)
async def autopilots_page(request: Request):
    """Kelola tugas agent terjadwal (terinspirasi Autopilots Multica).

    Menampilkan jadwal, riwayat run, dan proposal aksi destruktif yang menunggu
    persetujuan (autopilot tidak pernah mengeksekusi aksi destruktif sendiri, §17).
    """
    autopilots = await autopilot_store.list_all()
    runs = await autopilot_store.recent_runs()
    # Proposal yang diantri autopilot (decision='proposal:pending') — menunggu tinjauan.
    proposals = await db.fetchall(
        """SELECT id, session_id, tool_name, tool_input, created_at
           FROM approval_log WHERE decision='proposal:pending'
           ORDER BY id DESC LIMIT 30"""
    )
    return templates.TemplateResponse(
        request,
        "autopilots.html",
        {
            "autopilots": autopilots,
            "runs": runs,
            "proposals": proposals,
            "roles": available_roles(),
        },
    )


@app.post("/autopilots")
async def autopilots_create(request: Request):
    """Buat autopilot baru. interval_unit (menit/jam/hari) → detik."""
    form = await request.form()
    name = (form.get("name") or "").strip()
    role = (form.get("role") or "").strip()
    prompt = (form.get("prompt") or "").strip()
    try:
        every = int(form.get("every") or 0)
    except (ValueError, TypeError):
        every = 0
    unit = (form.get("unit") or "hour").strip()
    factor = {"minute": 60, "hour": 3600, "day": 86400}.get(unit, 3600)
    interval_sec = every * factor
    # Validasi: role harus dikenal, field wajib terisi, interval masuk akal.
    if not name or not prompt or role not in available_roles() or interval_sec <= 0:
        return RedirectResponse(url="/autopilots", status_code=303)
    ap_id = await autopilot_store.create(name, role, prompt, interval_sec)
    log.info("autopilot_created", autopilot=ap_id, role=role, interval_sec=interval_sec)
    return RedirectResponse(url="/autopilots", status_code=303)


@app.post("/autopilots/toggle")
async def autopilots_toggle(request: Request):
    """Aktif/jeda autopilot."""
    form = await request.form()
    try:
        ap_id = int(form.get("autopilot_id") or 0)
    except (ValueError, TypeError):
        ap_id = 0
    enabled = (form.get("enabled") or "") == "1"
    if ap_id:
        await autopilot_store.set_enabled(ap_id, enabled)
    return RedirectResponse(url="/autopilots", status_code=303)


@app.post("/autopilots/delete")
async def autopilots_delete(request: Request):
    """Hapus autopilot."""
    form = await request.form()
    try:
        ap_id = int(form.get("autopilot_id") or 0)
    except (ValueError, TypeError):
        ap_id = 0
    if ap_id:
        await autopilot_store.delete(ap_id)
    return RedirectResponse(url="/autopilots", status_code=303)


@app.get("/router", response_class=HTMLResponse)
async def router_page(request: Request, saved: bool = False):
    """Editor peta tier→model. Router tetap pilih tier; user pilih model tiap tier."""
    active = await RouterConfigStore(db).get_map()  # {Complexity: (model, provider, cost)}
    overridden = await RouterConfigStore(db).is_overridden()
    # Susun baris per tier (urut kompleksitas) untuk template.
    tiers = []
    for tier in Complexity:
        model, provider, _ = active[tier]
        default_model, default_provider, _ = SmartRouter.MODELS[tier]
        tiers.append(
            {
                "key": tier.value,
                "label": tier.value.upper(),
                "model": model,
                "provider": provider,
                "is_default": (model == default_model and provider == default_provider),
            }
        )
    return templates.TemplateResponse(
        request,
        "router.html",
        {
            "tiers": tiers,
            "known_models": KNOWN_MODELS,
            "overridden": overridden,
            "saved": saved,
        },
    )


@app.post("/router")
async def router_save(request: Request):
    """Simpan peta tier→model. Tiap tier kirim 'tier_<key>' berformat 'provider|model'."""
    form = await request.form()
    if (form.get("action") or "") == "reset":
        await RouterConfigStore(db).reset()
        log.info("router_map_reset")
        return RedirectResponse(url="/router?saved=true", status_code=303)
    mapping: dict[str, dict[str, str]] = {}
    for tier in Complexity:
        choice = (form.get(f"tier_{tier.value}") or "").strip()
        provider, _, model = choice.partition("|")
        if provider and model:
            mapping[tier.value] = {"provider": provider, "model": model}
    result = await RouterConfigStore(db).set_map(mapping)
    log.info("router_map_saved", **result)
    return RedirectResponse(url="/router?saved=true", status_code=303)


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
