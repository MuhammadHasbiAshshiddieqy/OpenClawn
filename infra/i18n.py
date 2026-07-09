"""UI locale (bahasa tampilan web) — default English, opsional Indonesian.

Terpisah dari locale AGENT (§1.5, respons agent selalu mengikuti bahasa user
apa adanya — TIDAK diubah oleh modul ini). Ini murni untuk teks STATIS di
web/templates/*.html: label nav, tombol, judul halaman, pesan status.

Pola sama seperti `compaction_mode` (infra/settings.py): key-value tunggal di
app_settings, dibaca via SettingsStore, tanpa tabel/dependency baru.

STRINGS adalah dict datar {key: {"en": ..., "id": ...}}. Key dikelompokkan per
namespace (nav.*, common.*, index.*, dst) sesuai halaman pemakainya, supaya
mudah ditelusuri saat menambah string baru.
"""

DEFAULT_LOCALE = "en"
LOCALES = ("en", "id")

STRINGS: dict[str, dict[str, str]] = {
    # ── Sidebar nav (dipakai di semua halaman) ──────────────────────────────
    "nav.chat": {"en": "Chat", "id": "Obrolan"},
    "nav.activity": {"en": "Activity", "id": "Aktivitas"},
    "nav.autopilots": {"en": "Autopilots", "id": "Autopilot"},
    "nav.mcp": {"en": "MCP", "id": "MCP"},
    "nav.metrics": {"en": "Metrics", "id": "Metrik"},
    "nav.skills": {"en": "Skills", "id": "Skill"},
    "nav.conversations": {"en": "Conversations", "id": "Percakapan"},
    "nav.router": {"en": "Router", "id": "Router"},
    "nav.settings": {"en": "Settings", "id": "Pengaturan"},
    "nav.workspace": {"en": "Workspace", "id": "Ruang Kerja"},
    "nav.roles": {"en": "Roles", "id": "Peran"},
    "nav.single_agent": {"en": "single-agent", "id": "agen-tunggal"},
    # ── Umum (dipakai di banyak halaman) ─────────────────────────────────────
    "common.saved": {"en": "Saved.", "id": "Tersimpan."},
    "common.save": {"en": "Save", "id": "Simpan"},
    "common.cancel": {"en": "Cancel", "id": "Batalkan"},
    "common.apply": {"en": "Apply", "id": "Terapkan"},
    "common.revert": {"en": "Revert", "id": "Batalkan"},
    "common.reset_default": {"en": "Reset to default", "id": "Reset ke default"},
    "common.no_data_yet": {"en": "No data yet.", "id": "Belum ada data."},
    "common.active": {"en": "active", "id": "aktif"},
    "common.model": {"en": "Model", "id": "Model"},
    "common.auto_router": {"en": "Auto (router)", "id": "Otomatis (router)"},
    # ── Chat (index.html) ────────────────────────────────────────────────────
    "index.hello": {"en": "Hi, I'm", "id": "Halo, saya"},
    "index.intro": {
        "en": "A nimble yet sharp AI agent — with 27 tools to explore code, "
        "inspect git, search the web, write documents (docx/pdf), and execute "
        "tasks safely.",
        "id": "Agent AI yang lincah sekaligus tajam — punya 27 tool untuk "
        "menjelajah kode, inspeksi git, mencari di web, menulis dokumen "
        "(docx/pdf), dan mengeksekusi tugas dengan aman.",
    },
    "index.suggest_explore": {"en": "Explore folder", "id": "Jelajahi folder"},
    "index.suggest_search": {"en": "Search code", "id": "Cari di kode"},
    "index.suggest_intro": {"en": "Introduce yourself", "id": "Perkenalan"},
    "index.mode_label": {"en": "Mode", "id": "Mode"},
    "index.mode_single": {"en": "Single — 1 agent", "id": "Single — 1 agent"},
    "index.mode_pipeline": {
        "en": "Pipeline — sequential handoff",
        "id": "Pipeline — handoff berurutan",
    },
    "index.mode_debate": {"en": "Debate — N-round discussion", "id": "Debate — diskusi N ronde"},
    "index.mode_orchestrator": {
        "en": "Orchestrator — lead delegates",
        "id": "Orchestrator — lead delegasi",
    },
    "index.convo_hint": {
        "en": "Agents will chat with each other automatically",
        "id": "Agent akan saling mengobrol otomatis",
    },
    "index.workdir_title": {
        "en": "Working directory for this session (empty = server default)",
        "id": "Folder kerja untuk sesi ini (kosong = default server)",
    },
    "index.workdir_placeholder": {
        "en": "Working directory (optional)",
        "id": "Folder kerja (opsional)",
    },
    "index.trust_title": {
        "en": "Trust mode: skip manual approval for this session (code_run always still requires it)",
        "id": "Trust mode: lewati approval manual untuk sesi ini (code_run tetap selalu perlu)",
    },
    "index.trust_label": {"en": "Trust", "id": "Percaya"},
    "index.new_chat": {"en": "New chat", "id": "Chat baru"},
    "index.history": {"en": "History", "id": "Riwayat"},
    "index.history_empty": {"en": "No chats yet", "id": "Belum ada chat"},
    "index.history_delete": {"en": "Delete", "id": "Hapus"},
    "index.history_delete_confirm": {
        "en": "Delete this chat? This cannot be undone.",
        "id": "Hapus chat ini? Tindakan ini tidak bisa dibatalkan.",
    },
    "index.bucket_today": {"en": "Today", "id": "Hari ini"},
    "index.bucket_yesterday": {"en": "Yesterday", "id": "Kemarin"},
    "index.bucket_7d": {"en": "Previous 7 days", "id": "7 hari terakhir"},
    "index.bucket_30d": {"en": "Previous 30 days", "id": "30 hari terakhir"},
    "index.bucket_older": {"en": "Older", "id": "Lebih lama"},
    "index.participants": {"en": "Participants", "id": "Peserta"},
    "index.order": {"en": "Order", "id": "Urutan"},
    "index.lead_workers": {"en": "Lead + Workers", "id": "Lead + Workers"},
    "index.rounds": {"en": "Rounds", "id": "Ronde"},
    "index.lead_hint": {
        "en": "Click a chip to make it the",
        "id": "Klik chip untuk menetapkannya sebagai",
    },
    "index.composer_placeholder": {"en": "Ask CLAWN anything…", "id": "Tanya apa saja ke CLAWN…"},
    "index.composer_hint": {
        "en": "Enter to send · Shift+Enter for new line",
        "id": "Enter untuk kirim · Shift+Enter baris baru",
    },
    "index.agent_suffix": {"en": "Agent", "id": "Agent"},
    "index.suggest_explore_q": {
        "en": "What's in this folder? Use list_dir then explain its structure.",
        "id": "Apa isi folder ini? Gunakan list_dir lalu jelaskan strukturnya.",
    },
    "index.suggest_search_q": {
        "en": "Find all async functions in the codebase with grep.",
        "id": "Cari semua fungsi async di codebase dengan grep.",
    },
    "index.suggest_intro_q": {
        "en": "Summarize what OpenCLAWN is in 3 points.",
        "id": "Ringkas apa itu OpenCLAWN dalam 3 poin.",
    },
    "index.answer_placeholder": {
        "en": "Answer the agent's question…",
        "id": "Jawab pertanyaan agent…",
    },
    "index.pipeline_desc": {
        "en": "sequential handoff between roles",
        "id": "handoff berurutan antar role",
    },
    "index.debate_desc": {
        "en": "multi-role discussion, several rounds",
        "id": "diskusi multi-peran beberapa ronde",
    },
    "index.orchestrator_desc": {
        "en": "lead delegates to workers",
        "id": "lead mendelegasikan ke worker",
    },
    "index.status_route": {"en": "Route", "id": "Route"},
    "index.status_routing": {"en": "Choosing model… ({detail})", "id": "Memilih model… ({detail})"},
    "index.status_think": {"en": "Think", "id": "Think"},
    "index.status_thinking": {"en": "Thinking…", "id": "Berpikir…"},
    "index.status_tool": {"en": "Tool", "id": "Tool"},
    "index.status_trusted": {"en": "Trusted", "id": "Dipercaya"},
    "index.status_ask": {"en": "Ask", "id": "Ask"},
    "index.status_approval": {"en": "Approval needed", "id": "Butuh persetujuan"},
    "index.approve": {"en": "Approve", "id": "Setujui"},
    "index.reject": {"en": "Reject", "id": "Tolak"},
    "index.approved": {"en": "Approved", "id": "Disetujui"},
    "index.rejected": {"en": "Rejected", "id": "Ditolak"},
    "index.approval_failed": {
        "en": "Failed to send decision, try again",
        "id": "Gagal mengirim keputusan, coba lagi",
    },
    "index.status_fall": {"en": "Fallback", "id": "Fallback"},
    "index.status_fallback_to": {"en": "to {detail}", "id": "ke {detail}"},
    "index.status_stop": {"en": "Halt", "id": "Halt"},
    "index.status_loop_stopped": {
        "en": "Loop stopped: {detail} called repeatedly",
        "id": "Loop dihentikan: {detail} dipanggil berulang",
    },
    "index.status_info": {"en": "Info", "id": "Info"},
    "index.status_wait": {"en": "Wait", "id": "Wait"},
    "index.status_sending": {"en": "Sending…", "id": "Mengirim…"},
    "index.status_reasoning": {"en": "Reasoning…", "id": "Menalar…"},
    "index.status_gen": {"en": "Gen", "id": "Gen"},
    "index.status_writing": {"en": "Writing answer…", "id": "Menulis jawaban…"},
    "index.status_no_response": {
        "en": "Still working (model is taking a while)…",
        "id": "Masih bekerja (model butuh waktu)…",
    },
    "index.status_err": {"en": "Err", "id": "Err"},
    "index.status_file": {"en": "File", "id": "File"},
    "index.download_file": {"en": "Download", "id": "Unduh"},
    "index.error_no_answer": {
        "en": "No answer (all models failed/empty)",
        "id": "Tidak ada jawaban (semua model gagal/kosong)",
    },
    "index.error_disconnected": {"en": "Connection lost", "id": "Koneksi terputus"},
    "index.status_start": {"en": "Start", "id": "Start"},
    "index.status_starting_convo": {"en": "Starting conversation…", "id": "Memulai percakapan…"},
    "index.status_answering": {"en": "{role} is answering…", "id": "{role} sedang menjawab…"},
    "index.status_sela": {"en": "Interject", "id": "Sela"},
    "index.status_interjecting": {
        "en": "Interjecting — will be considered next turn…",
        "id": "Menyela — akan diperhitungkan giliran berikutnya…",
    },
    "index.status_continuing": {"en": "Continuing…", "id": "Melanjutkan…"},
    "index.status_halt": {"en": "Halt", "id": "Halt"},
    "index.convo_stopped": {"en": "Conversation stopped", "id": "Percakapan dihentikan"},
    "index.convo_max_turns": {"en": "Turn limit reached", "id": "Batas giliran tercapai"},
    "index.convo_strategy_done": {"en": "Conversation finished", "id": "Percakapan selesai"},
    "index.convo_done_generic": {"en": "Done: {reason}", "id": "Selesai: {reason}"},
    "index.thinking_label": {"en": "Thinking…", "id": "Thinking…"},
    "index.thinking_done": {"en": "Thinking · done", "id": "Thinking · selesai"},
    "index.err_debate_min": {
        "en": "Debate needs at least 2 participants",
        "id": "Debate butuh minimal 2 peserta",
    },
    "index.err_orchestrator_min": {
        "en": "Orchestrator needs a lead + at least 1 worker",
        "id": "Orchestrator butuh lead + minimal 1 worker",
    },
    "index.err_pipeline_min": {
        "en": "Pipeline needs at least 1 participant",
        "id": "Pipeline butuh minimal 1 peserta",
    },
    "index.toggle_sidebar": {"en": "Toggle sidebar", "id": "Toggle sidebar"},
    "index.send_title": {"en": "Send (Enter)", "id": "Kirim (Enter)"},
    "index.stop_title": {"en": "Stop", "id": "Hentikan"},
    "index.budget_title": {
        "en": "Context window used in the last turn",
        "id": "Context window terpakai turn terakhir",
    },
    "index.reduce": {"en": "Decrease", "id": "Kurangi"},
    "index.add": {"en": "Add", "id": "Tambah"},
    "index.lead_title": {"en": "Lead", "id": "Lead"},
    "index.copy": {"en": "Copy", "id": "Copy"},
    "index.copied": {"en": "Copied!", "id": "Copied!"},
    "index.copy_failed": {"en": "Failed", "id": "Gagal"},
    "index.drop_hint": {
        "en": "Drop a text file to insert it",
        "id": "Lepas file teks untuk disisipkan",
    },
    "index.file_too_large": {
        "en": "File too large (max {kib} KiB)",
        "id": "File terlalu besar (maks {kib} KiB)",
    },
    "index.file_inserted": {
        "en": 'File "{name}" inserted — review then send',
        "id": 'File "{name}" disisipkan — tinjau lalu kirim',
    },
    "index.file_read_failed": {"en": "Failed to read file", "id": "Gagal membaca file"},
    "index.file_content_label": {"en": "Contents of file", "id": "Isi file"},
    "index.error_http": {"en": "HTTP Error", "id": "Error HTTP"},
    "index.token_turns": {"en": "turns", "id": "giliran"},
    "index.token_label": {"en": "tokens", "id": "token"},
    "index.status_warn": {"en": "Warn", "id": "Warn"},
    # ── Activity ──────────────────────────────────────────────────────────────
    "activity.title": {"en": "Activity", "id": "Aktivitas"},
    "activity.subtitle": {
        "en": "the evidence trail behind every agent decision — routing · tool · handoff · conversation · crystallize",
        "id": "berkas bukti tiap keputusan agent — routing · tool · handoff · conversation · crystallize",
    },
    "activity.case_note": {
        "en": "Every row is a traceable piece of evidence: what was used, what happened, and the outcome — not just a log.",
        "id": "Setiap baris adalah satu bukti yang bisa ditelusuri: apa yang dipakai, apa yang terjadi, dan hasilnya — bukan sekadar log.",
    },
    "activity.all_roles": {"en": "All roles", "id": "Semua peran"},
    "activity.no_events": {"en": "No activity yet", "id": "Belum ada aktivitas"},
    "activity.no_events_for_role": {"en": "for role", "id": "untuk peran"},
    "activity.no_events_hint": {
        "en": "Start a conversation in Chat to populate the timeline.",
        "id": "Mulai percakapan di Chat agar linimasa terisi.",
    },
    "activity.open_blockers": {"en": "Open blockers", "id": "Hambatan terbuka"},
    "activity.resolve": {"en": "Resolve", "id": "Selesai"},
    # ── Metrics ───────────────────────────────────────────────────────────────
    "metrics.title": {"en": "Routing Calibration", "id": "Routing Calibration"},
    "metrics.subtitle": {
        "en": "router decision audit — Innovation #1",
        "id": "audit keputusan router — Inovasi #1",
    },
    "metrics.no_data": {
        "en": "No data yet. Start chatting first!",
        "id": "Belum ada data. Mulai mengobrol dulu!",
    },
    "metrics.tuning_recs": {"en": "Tuning Recommendations", "id": "Tuning Recommendations"},
    "metrics.active_offset": {"en": "Active threshold offset:", "id": "Offset threshold aktif:"},
    "metrics.offset_upgrade": {
        "en": "router upgrades tier sooner",
        "id": "router naik tier lebih cepat",
    },
    "metrics.offset_downgrade": {
        "en": "router stays on cheaper tier longer",
        "id": "router bertahan tier murah lebih lama",
    },
    "metrics.offset_neutral": {
        "en": "original router behavior (not yet calibrated)",
        "id": "perilaku router asli (belum dikalibrasi)",
    },
    "metrics.not_enough_data": {
        "en": "Not enough audit data yet ({n} events). Recommendations appear once enough routing decisions accumulate.",
        "id": "Data audit belum cukup ({n} events). Rekomendasi muncul setelah cukup banyak keputusan routing terkumpul.",
    },
    "metrics.well_calibrated": {
        "en": "Routing looks well calibrated — no suggested changes.",
        "id": "Routing tampak terkalibrasi baik — tidak ada saran perubahan.",
    },
    "metrics.apply_suggestion": {
        "en": "Apply suggestion ({delta} to offset)",
        "id": "Terapkan saran ({delta} ke offset)",
    },
    "metrics.apply_note": {
        "en": "Changes router behavior — logged in history & revertible.",
        "id": "Mengubah perilaku router — tercatat di riwayat & bisa di-revert.",
    },
    "metrics.no_clear_direction": {
        "en": "Suggestions cancel out; no single clear direction.",
        "id": "Saran saling meniadakan; tidak ada arah geser tunggal yang jelas.",
    },
    "metrics.history": {"en": "Calibration History", "id": "Riwayat Kalibrasi"},
    "metrics.tool_usage": {"en": "Tool Usage", "id": "Penggunaan Tool"},
    "metrics.no_tools": {
        "en": "No tools called yet. Stats appear once the agent uses a tool.",
        "id": "Belum ada tool dipanggil. Statistik muncul setelah agent memakai tool.",
    },
    "metrics.of": {"en": "of", "id": "dari"},
    "metrics.events": {"en": "events", "id": "events"},
    "metrics.time": {"en": "Time", "id": "Waktu"},
    "metrics.from": {"en": "From", "id": "Dari"},
    "metrics.to": {"en": "To", "id": "Ke"},
    "metrics.source": {"en": "Source", "id": "Sumber"},
    "metrics.reason": {"en": "Reason", "id": "Alasan"},
    "metrics.complexity": {"en": "Complexity", "id": "Complexity"},
    "metrics.total": {"en": "Total", "id": "Total"},
    "metrics.corrections": {"en": "Corrections", "id": "Corrections"},
    "metrics.correction_rate": {"en": "Correction Rate", "id": "Correction Rate"},
    "metrics.avg_cost": {"en": "Avg Cost (USD)", "id": "Avg Cost (USD)"},
    "metrics.auto_tune": {"en": "auto-tune", "id": "auto-tune"},
    "metrics.auto_tune_title": {
        "en": "Auto-apply opt-in (config). Still clamped to ±1 & revertible.",
        "id": "Auto-apply opt-in (config). Tetap clamp ±1 & revertible.",
    },
    "metrics.tool": {"en": "Tool", "id": "Tool"},
    "metrics.used": {"en": "Used", "id": "Dipakai"},
    "metrics.error": {"en": "Error", "id": "Error"},
    "metrics.timeout": {"en": "Timeout", "id": "Timeout"},
    "metrics.fail_rate": {"en": "Fail Rate", "id": "Fail Rate"},
    "metrics.avg_latency": {"en": "Avg Latency", "id": "Avg Latency"},
    # ── Router ────────────────────────────────────────────────────────────────
    "router.title": {"en": "Router Model Map", "id": "Router Model Map"},
    "router.subtitle": {
        "en": "choose a model per tier — the router still decides the tier automatically",
        "id": "pilih model tiap tier — router tetap memutuskan tier otomatis",
    },
    "router.explain": {
        "en": "SmartRouter scores each request's complexity and picks a {tier} "
        "(TRIVIAL→CRITICAL). Here you choose the {model} for each tier. If the "
        "local model is offline, the fallback chain is used automatically.",
        "id": "SmartRouter menilai kompleksitas tiap permintaan lalu memilih "
        "{tier} (TRIVIAL→CRITICAL). Di sini kamu menentukan {model} untuk "
        "tiap tier. Bila model lokal sedang offline, fallback chain otomatis "
        "dipakai.",
    },
    "router.custom_map_active": {"en": "Custom map active.", "id": "Peta kustom aktif."},
    "router.default_map": {
        "en": "Currently using the default map.",
        "id": "Saat ini memakai peta default.",
    },
    "router.tier": {"en": "Tier", "id": "Tier"},
    "router.save_map": {"en": "Save map", "id": "Simpan peta"},
    # ── Settings ──────────────────────────────────────────────────────────────
    "settings.title": {"en": "Settings", "id": "Settings"},
    "settings.subtitle": {
        "en": "model override · context compaction",
        "id": "override model · context compaction",
    },
    "settings.explain": {
        "en": "By default, OpenCLAWN automatically picks a model per query "
        "complexity (smart router — Innovation #1). The override below "
        "forces ALL queries to one model — useful for experiments, e.g. "
        "using Gemini only.",
        "id": "Secara default, OpenCLAWN memilih model otomatis per kompleksitas "
        "query (router cerdas — Inovasi #1). Override di bawah memaksa "
        "semua query ke satu model — berguna untuk eksperimen, mis. "
        "memakai Gemini saja.",
    },
    "settings.active_model": {"en": "Active model", "id": "Model aktif"},
    "settings.auto_recommended": {
        "en": "Automatic (router picks per complexity) — recommended",
        "id": "Otomatis (router memilih per kompleksitas) — disarankan",
    },
    "settings.compaction": {
        "en": "Context compaction (headroom)",
        "id": "Context compaction (headroom)",
    },
    "settings.compaction_off": {
        "en": "Off — drop old turns (default, safe)",
        "id": "Off — potong turn lama (default, aman)",
    },
    "settings.compaction_local": {
        "en": "Local — summarize old turns via local model (free, private)",
        "id": "Local — ringkas turn lama via model lokal (gratis, privat)",
    },
    "settings.compaction_cloud": {
        "en": "Cloud — summarize via cloud model (quality, for complex history)",
        "id": "Cloud — ringkas via model cloud (kualitas, untuk history kompleks)",
    },
    "settings.status_override": {
        "en": "Status: override active →",
        "id": "Status: override aktif →",
    },
    "settings.status_override_note": {
        "en": "All queries use this model.",
        "id": "Semua query memakai model ini.",
    },
    "settings.status_auto": {"en": "Status:", "id": "Status:"},
    "settings.status_auto_mode": {"en": "automatic mode", "id": "mode otomatis"},
    "settings.status_auto_note": {
        "en": "Router picks a model per query.",
        "id": "Router memilih model per query.",
    },
    "settings.ui_language": {"en": "UI language", "id": "Bahasa tampilan"},
    "settings.ui_language_note": {
        "en": "Only changes labels/buttons on this dashboard. The agent always replies in the language YOU write in — this does not change that.",
        "id": "Hanya mengubah label/tombol di dashboard ini. Agent tetap membalas sesuai bahasa yang KAMU pakai — ini tidak mengubah itu.",
    },
    "settings.save": {"en": "Save", "id": "Simpan"},
    "settings.compaction_note": {
        "en": "<strong>Compaction</strong> (inspired by <em>headroom</em>): when the context "
        "window fills up, the default drops the oldest turns (what's dropped is truly gone). "
        "<code>local</code>/<code>cloud</code> mode summarizes old turns into one block instead "
        "of discarding — saves tokens without losing all context. Default is <code>off</code> "
        "because LLM summarization adds latency & can lose nuance; enable it if your "
        "conversations run long.",
        "id": "<strong>Compaction</strong> (terinspirasi <em>headroom</em>): saat context window "
        "penuh, default memotong turn terlama (yang hilang benar-benar hilang). Mode "
        "<code>local</code>/<code>cloud</code> meringkas turn lama jadi satu blok alih-alih "
        "membuang — hemat token tanpa kehilangan konteks total. Default <code>off</code> "
        "karena peringkasan via LLM menambah latensi & bisa membuang nuansa; nyalakan "
        "jika percakapanmu panjang.",
    },
    "settings.footer_note": {
        "en": "Note: cloud models (Claude/Gemini) need an API key in <code>.env</code> "
        "(<code>ANTHROPIC_API_KEY</code> / <code>GOOGLE_API_KEY</code>). Local models "
        "(gemma4) need Ollama running with the model already <code>pull</code>ed. "
        "If the selected model is unavailable, OpenCLAWN falls back to the fallback chain.",
        "id": "Catatan: model cloud (Claude/Gemini) butuh API key di <code>.env</code> "
        "(<code>ANTHROPIC_API_KEY</code> / <code>GOOGLE_API_KEY</code>). Model lokal "
        "(gemma4) butuh Ollama berjalan dan model sudah di-<code>pull</code>. "
        "Jika model terpilih tidak tersedia, OpenCLAWN turun ke fallback chain.",
    },
    "settings.compaction_label": {"en": "Compaction:", "id": "Compaction:"},
    # ── Skills ────────────────────────────────────────────────────────────────
    "skills.title": {"en": "Skill Decay", "id": "Skill Decay"},
    "skills.subtitle": {
        "en": "skills fade exponentially (base {base}) — Innovation #2",
        "id": "skill memudar eksponensial (base {base}) — Inovasi #2",
    },
    "skills.active": {"en": "active", "id": "aktif"},
    "skills.draft": {"en": "draft", "id": "draft"},
    "skills.archived": {"en": "archived", "id": "arsip"},
    "skills.threshold_note": {
        "en": "Score below {threshold} ({pct}%) → archived; used again → revives.",
        "id": "Skor di bawah {threshold} ({pct}%) → diarsipkan; dipakai lagi → revive.",
    },
    "skills.packs_summary": {
        "en": "Skill packs — export & import (share across installs)",
        "id": "Skill packs — ekspor & impor (berbagi antar-instalasi)",
    },
    "skills.export": {"en": "Export", "id": "Ekspor"},
    "skills.export_hint": {
        "en": "Download active skills as a Markdown file to share.",
        "id": "Unduh skill aktif sebagai berkas Markdown untuk dibagikan.",
    },
    "skills.export_all": {"en": "All", "id": "Semua"},
    "skills.import": {"en": "Import", "id": "Impor"},
    "skills.import_hint": {
        "en": "Comes in as <b>draft</b> (security-scanned, not auto-used — activate manually).",
        "id": "Masuk sebagai <b>draft</b> (discan keamanan, tidak otomatis dipakai — Anda aktifkan manual).",
    },
    "skills.import_paste_placeholder": {
        "en": "Paste skill pack contents (Markdown)…",
        "id": "Tempel isi skill pack (Markdown)…",
    },
    "skills.import_url_placeholder": {
        "en": "or pack URL (https://…)",
        "id": "atau URL pack (https://…)",
    },
    "skills.original_role": {"en": "original role", "id": "role asli"},
    "skills.import_btn": {"en": "Import", "id": "Impor"},
    "skills.no_skills": {
        "en": "No skills crystallized yet. Skills are born when the agent judges its solution worth saving (Innovation #3).",
        "id": "Belum ada skill terkristalisasi. Skill lahir saat agent menilai solusinya layak disimpan (Inovasi #3).",
    },
    "skills.col_skill": {"en": "Skill", "id": "Skill"},
    "skills.col_role": {"en": "Role", "id": "Role"},
    "skills.col_status": {"en": "Status", "id": "Status"},
    "skills.col_decay": {"en": "Decay (projected)", "id": "Decay (terproyeksi)"},
    "skills.col_idle": {"en": "Idle", "id": "Idle"},
    "skills.col_used": {"en": "Used", "id": "Dipakai"},
    "skills.col_confidence": {"en": "Confidence", "id": "Confidence"},
    "skills.score_title": {
        "en": "score {score} (archive threshold {threshold})",
        "id": "skor {score} (ambang arsip {threshold})",
    },
    "skills.crystallization_title": {
        "en": "Crystallization (Innovation #3)",
        "id": "Kristalisasi (Inovasi #3)",
    },
    "skills.crystallization_note": {
        "en": "The agent judges the quality of its own solution before saving it as a skill. "
        "Confidence ≥ {threshold}/5 <b>and</b> no critical gaps → <b>active</b>; otherwise → "
        "<b>draft</b> (excluded from auto-context). Evaluator is at least as strong as the generator.",
        "id": "Agent menilai kualitas solusinya sebelum menyimpan sebagai skill. Confidence ≥ "
        "{threshold}/5 <b>dan</b> tanpa gap kritis → <b>active</b>; selain itu → "
        "<b>draft</b> (tak masuk auto-context). Evaluator minimal setara generator.",
    },
    "skills.no_crystallization": {
        "en": "No crystallization attempts yet. Appears once the agent finishes a multi-tool task.",
        "id": "Belum ada percobaan kristalisasi. Muncul setelah agent menyelesaikan tugas multi-tool.",
    },
    "skills.col_critical_gaps": {"en": "Critical gaps", "id": "Gap kritis"},
    "skills.yes": {"en": "yes", "id": "ya"},
    "skills.col_gen_eval": {"en": "Generator → Evaluator", "id": "Generator → Evaluator"},
    "skills.col_reason": {"en": "Reason", "id": "Alasan"},
    "skills.curation_title": {
        "en": "Curation — skill consolidation (I1)",
        "id": "Curation — konsolidasi skill (I1)",
    },
    "skills.curation_note": {
        "en": "Similar skills are proposed for merging (gated judge ≥ {threshold}/5) so the "
        "library doesn't fragment. Default (<code>curation_auto=False</code>): proposals wait "
        "for a human to click <b>Apply</b>. The loser is <b>not deleted</b> (status "
        "<code>merged</code>) — every applied merge <b>can be reverted</b>.",
        "id": "Skill mirip diusulkan untuk digabung (gated judge ≥ {threshold}/5) "
        "agar library tak terfragmentasi. Default (<code>curation_auto=False</code>): usulan "
        "menunggu tombol <b>Terapkan</b> manusia. Loser <b>tidak dihapus</b> (status "
        "<code>merged</code>) — setiap merge yang diterapkan <b>dapat dibatalkan</b>.",
    },
    "skills.no_curation": {
        "en": "No consolidation yet. Runs automatically (throttled ~1×/day) when skills overlap.",
        "id": "Belum ada konsolidasi. Berjalan otomatis (throttled ~1×/hari) saat ada skill tumpang tindih.",
    },
    "skills.col_action": {"en": "Action", "id": "Aksi"},
    "skills.col_winner": {"en": "Winner", "id": "Winner"},
    "skills.col_absorbed": {"en": "Absorbed", "id": "Diserap"},
    "skills.col_similarity": {"en": "Similarity", "id": "Similarity"},
    "skills.col_judge": {"en": "Judge", "id": "Judge"},
    "skills.apply": {"en": "Apply", "id": "Terapkan"},
    "skills.revert": {"en": "Revert", "id": "Batalkan"},
    # ── Conversations ─────────────────────────────────────────────────────────
    "conversations.title": {"en": "Conversations", "id": "Percakapan"},
    "conversations.subtitle": {
        "en": "multi-agent conversation archive — pipeline · debate · orchestrator",
        "id": "arsip percakapan multi-agent — pipeline · debate · orchestrator",
    },
    "conversations.no_archive": {
        "en": "No archived conversations yet.",
        "id": "Belum ada percakapan yang diarsipkan.",
    },
    "conversations.no_archive_hint": {
        "en": "Start Pipeline/Debate/Orchestrator mode in Chat.",
        "id": "Mulai mode Pipeline/Debate/Orchestrator di Chat.",
    },
    "conversations.turns": {"en": "turns", "id": "giliran"},
    # ── Autopilots ────────────────────────────────────────────────────────────
    "autopilots.title": {"en": "Autopilots", "id": "Autopilots"},
    "autopilots.subtitle": {
        "en": "scheduled agent tasks — runs read-only; destructive actions become proposals",
        "id": "tugas agent terjadwal — berjalan read-only; aksi destruktif jadi proposal",
    },
    "autopilots.safety_note": {
        "en": "<b>Safe by design:</b> autopilot runs without you present. Read-only tools "
        "(research, audit, summaries) run directly; actions that need approval "
        "<b>are not executed</b> — they're queued as a <b>proposal</b> below for you to review.",
        "id": "<b>Aman by design:</b> autopilot berjalan tanpa Anda di depan. Tool read-only "
        "(riset, audit, ringkasan) dijalankan langsung; aksi yang butuh persetujuan "
        "<b>tidak dieksekusi</b> — diantri sebagai <b>proposal</b> di bawah untuk Anda tinjau.",
    },
    "autopilots.new_schedule": {"en": "New schedule", "id": "Jadwal baru"},
    "autopilots.name_placeholder": {
        "en": "Name (e.g. Daily security audit)",
        "id": "Nama (mis. Audit keamanan harian)",
    },
    "autopilots.prompt_placeholder": {
        "en": "Task instructions (e.g. 'Audit project dependencies & summarize security risk')",
        "id": "Instruksi tugas (mis. 'Audit dependency proyek & ringkas risiko keamanan')",
    },
    "autopilots.every": {"en": "Every", "id": "Tiap"},
    "autopilots.unit_minute": {"en": "minute", "id": "menit"},
    "autopilots.unit_hour": {"en": "hour", "id": "jam"},
    "autopilots.unit_day": {"en": "day", "id": "hari"},
    "autopilots.create": {"en": "Create", "id": "Buat"},
    "autopilots.active_schedules": {"en": "Active schedules", "id": "Jadwal aktif"},
    "autopilots.no_autopilots": {
        "en": "No autopilots yet. Create one above.",
        "id": "Belum ada autopilot. Buat satu di atas.",
    },
    "autopilots.status_active": {"en": "active", "id": "aktif"},
    "autopilots.status_paused": {"en": "paused", "id": "jeda"},
    "autopilots.meta": {
        "en": "every {interval}s · last: {last} · next: {next}",
        "id": "tiap {interval}s · terakhir: {last} · berikutnya: {next}",
    },
    "autopilots.never": {"en": "never", "id": "belum"},
    "autopilots.pause": {"en": "Pause", "id": "Jeda"},
    "autopilots.activate": {"en": "Activate", "id": "Aktifkan"},
    "autopilots.confirm_delete": {
        "en": "Delete this autopilot?",
        "id": "Hapus autopilot ini?",
    },
    "autopilots.delete": {"en": "Delete", "id": "Hapus"},
    "autopilots.proposals_pending": {
        "en": "Proposals awaiting review ({n})",
        "id": "Proposal menunggu tinjauan ({n})",
    },
    "autopilots.proposals_hint": {
        "en": "Actions generated by autopilot that need approval. Review then approve on the Chat page (HITL).",
        "id": "Aksi yang dihasilkan autopilot tapi butuh persetujuan. Tinjau lalu setujui di halaman Chat (HITL).",
    },
    "autopilots.run_history": {"en": "Run history", "id": "Riwayat run"},
    "autopilots.no_runs": {"en": "No runs yet.", "id": "Belum ada run."},
    "autopilots.proposal_count": {"en": "proposal", "id": "proposal"},
    # ── MCP ───────────────────────────────────────────────────────────────────
    "mcp.title": {"en": "MCP Servers", "id": "MCP Servers"},
    "mcp.subtitle": {
        "en": "external tools via Model Context Protocol — always requires approval",
        "id": "tool eksternal via Model Context Protocol — selalu butuh approval",
    },
    "mcp.safety_note": {
        "en": "<b>Safe by design:</b> MCP servers are uncontrolled third-party code. "
        "Discovered tools <b>always require approval</b> (HITL); remote servers "
        "are SSRF-guarded. A role must allow it via <code>soul.toml</code> "
        "(<code>mcp__*</code> or <code>mcp__&lt;server&gt;__*</code>) — explicit opt-in.",
        "id": "<b>Aman by design:</b> server MCP adalah kode pihak ketiga tak terkendali. "
        "Tool yang ditemukan <b>selalu butuh persetujuan</b> (HITL); server remote "
        "di-guard anti-SSRF. Role harus mengizinkan via <code>soul.toml</code> "
        "(<code>mcp__*</code> atau <code>mcp__&lt;server&gt;__*</code>) — opt-in eksplisit.",
    },
    "mcp.add_server": {"en": "Add server", "id": "Tambah server"},
    "mcp.name_placeholder": {
        "en": "Unique name (e.g. github, filesystem)",
        "id": "Nama unik (mis. github, filesystem)",
    },
    "mcp.transport_stdio": {"en": "stdio (local subprocess)", "id": "stdio (subprocess lokal)"},
    "mcp.transport_http": {"en": "http (remote server)", "id": "http (server remote)"},
    "mcp.command_placeholder": {
        "en": "stdio: command + args (e.g. npx -y @modelcontextprotocol/server-filesystem /path)",
        "id": "stdio: command + args (mis. npx -y @modelcontextprotocol/server-filesystem /path)",
    },
    "mcp.url_placeholder": {
        "en": "http: MCP server URL (https://…)",
        "id": "http: URL server MCP (https://…)",
    },
    "mcp.add_and_load": {"en": "Add & load", "id": "Tambah & muat"},
    "mcp.registered_servers": {"en": "Registered servers", "id": "Server terdaftar"},
    "mcp.no_servers": {
        "en": "No MCP servers yet. Add one above (e.g. the official filesystem/GitHub server).",
        "id": "Belum ada server MCP. Tambah di atas (mis. server filesystem/GitHub resmi).",
    },
    "mcp.status_active": {"en": "active", "id": "aktif"},
    "mcp.status_paused": {"en": "paused", "id": "jeda"},
    "mcp.pause": {"en": "Pause", "id": "Jeda"},
    "mcp.activate": {"en": "Activate", "id": "Aktifkan"},
    "mcp.confirm_delete": {"en": "Delete this MCP server?", "id": "Hapus server MCP ini?"},
    "mcp.delete": {"en": "Delete", "id": "Hapus"},
    "mcp.tools_found": {"en": "Tools found ({n})", "id": "Tool ditemukan ({n})"},
    "mcp.no_tools": {
        "en": "No MCP tools loaded yet. Add a reachable, active server.",
        "id": "Belum ada tool MCP yang dimuat. Tambah server aktif yang dapat dijangkau.",
    },
    "mcp.approval": {"en": "approval", "id": "approval"},
    # ── Login (self-host auth) ───────────────────────────────────────────────
    "login.token_label": {"en": "Access token", "id": "Token akses"},
    "login.submit": {"en": "Sign in", "id": "Masuk"},
    "login.error": {"en": "Incorrect token. Try again.", "id": "Token salah. Coba lagi."},
    "login.hint": {
        "en": "Single-user instance — the token is set via OPENCLAWN_AUTH_TOKEN.",
        "id": "Instansi single-user — token diatur lewat OPENCLAWN_AUTH_TOKEN.",
    },
    "login.or": {"en": "or", "id": "atau"},
    "login.oidc_submit": {
        "en": "Sign in with SSO",
        "id": "Masuk dengan SSO",
    },
    "nav.logout": {"en": "Sign out", "id": "Keluar"},
    # ── Error pages ───────────────────────────────────────────────────────────
    "error.404_title": {"en": "Page not found", "id": "Halaman tidak ditemukan"},
    "error.404_body": {
        "en": "This page doesn't exist. Check the URL, or go back to Chat.",
        "id": "Halaman ini tidak ada. Periksa URL, atau kembali ke Chat.",
    },
    "error.500_title": {"en": "Something went wrong", "id": "Terjadi kesalahan"},
    "error.500_body": {
        "en": "An unexpected error occurred. It has been logged — try again shortly.",
        "id": "Terjadi kesalahan tak terduga. Sudah tercatat di log — coba lagi sesaat lagi.",
    },
    "error.back_home": {"en": "Back to Chat", "id": "Kembali ke Chat"},
    "error.rate_limited": {
        "en": "Too many requests. Please wait a moment before trying again.",
        "id": "Terlalu banyak permintaan. Tunggu sebentar sebelum coba lagi.",
    },
}


def t(key: str, locale: str, **kwargs: str) -> str:
    """Terjemahkan `key` ke `locale`. Key hilang / locale tak dikenal → fallback English.

    `kwargs` di-format ke string via `str.format` (mis. `t('metrics.of', locale, n=5)`).
    Fail-safe: key tak ada di STRINGS → kembalikan key itu sendiri (kelihatan di UI,
    gampang dilacak) alih-alih melempar exception dan mematikan seluruh halaman.
    """
    entry = STRINGS.get(key)
    if not entry:
        return key
    text = entry.get(locale) or entry.get(DEFAULT_LOCALE) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def translator(locale: str):
    """Buat closure `t(key, **kwargs)` dengan locale sudah terikat — dipakai di Jinja2 context."""
    normalized = locale if locale in LOCALES else DEFAULT_LOCALE

    def _t(key: str, **kwargs: str) -> str:
        return t(key, normalized, **kwargs)

    return _t
