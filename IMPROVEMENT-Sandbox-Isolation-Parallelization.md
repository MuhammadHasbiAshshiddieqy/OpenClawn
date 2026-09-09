# IMPROVEMENT: Sandbox Isolation & Subtask Parallelization Maturity

> Dibuat berdasarkan gap analysis OpenCLAWN vs Manus (sandbox VM-per-task, lifecycle sleep/wake/recycle, multi-agent paralel). Format mengikuti konvensi `IMPROVEMENT-*.md` yang sudah dipakai di repo.

---

## Ready-to-paste prompt (untuk coding agent yang punya akses ke repo OpenCLAWN)

```
Kamu sedang bekerja di repo OpenCLAWN (open-source agentic AI framework,
arsitektur saat ini sudah punya Docker sandbox dan multi-agent routing,
tapi belum ada isolasi per-subtask yang matang maupun eksekusi paralel
yang production-grade).

Tugas kamu: naikkan maturity level isolasi & paralelisasi subtask supaya
setara dengan sandbox model Manus (VM/container per unit kerja, lifecycle
sleep-wake-recycle, orkestrasi paralel dengan fault containment granular),
TANPA mengunci ke satu vendor cloud (tetap self-hostable, sejalan dengan
positioning anti-vendor-lock-in OpenCLAWN).

Langkah kerja:

1. Audit dulu implementasi Docker sandbox yang ada sekarang di
   `[path ke sandbox module]`. Cek: apakah satu sandbox dipakai untuk
   seluruh task, atau sudah ada pemisahan per-subtask? Apakah subtask
   berjalan sekuensial atau ada mekanisme paralel?

2. Rancang task graph berbasis DAG: orchestrator memecah goal jadi
   subtask dengan dependency eksplisit (bukan cuma list sekuensial),
   supaya subtask independen bisa dieksekusi bersamaan.

3. Implementasikan sandbox-per-subtask: tiap subtask dapat container/
   microVM terisolasi sendiri (bukan share satu sandbox besar), dengan
   resource limit (CPU/mem/network) dan permission scope sesuai jenis
   kerjanya (browser-only, code-exec, file-manager, dst — least privilege
   per tipe agent).

4. Bangun concurrency engine: worker pool/queue yang menjalankan subtask
   independen secara paralel dengan batas concurrency yang bisa
   dikonfigurasi, plus agregasi hasil balik ke orchestrator.

5. Tambahkan lifecycle management pada sandbox: create on-demand → sleep
   (preserve state, lepas resource compute) saat idle → recycle setelah
   idle policy tertentu (buat konfigurasi, bukan hardcode). Sertakan
   state snapshot supaya sandbox bisa di-resume.

6. Pastikan fault containment: kegagalan satu subtask sandbox tidak boleh
   merusak/menghentikan subtask sandbox lain. Tambahkan retry policy
   per-subtask (bukan retry seluruh task), dan circuit breaker untuk
   subtask yang gagal berulang.

7. Jaga context hygiene: tiap subagent hanya menerima context yang
   scoped ke tugasnya sendiri, hasilnya di-summarize balik ke
   orchestrator — jangan gabungkan raw history semua subtask jadi satu
   context besar.

8. Tambahkan observability: trace/log per-subtask sandbox yang bisa
   di-replay terpisah (align dengan konsep trajectory generation yang
   sudah dicatat di riset framework Hermes).

Constraint: pertimbangkan opsi isolasi bertingkat — Docker sebagai
default (low friction, sudah ada), dengan opsi upgrade ke gVisor atau
Firecracker microVM untuk deployment yang butuh isolasi lebih kuat
(enterprise/multi-tenant). Jangan buat ini all-or-nothing; buat sebagai
pluggable backend seperti pola trait system yang sudah dipakai di
framework lain yang diriset (swappable provider).

Keluarkan hasil sebagai: (a) ringkasan gap yang ditemukan di step 1,
(b) desain arsitektur (diagram/teks) untuk DAG orchestrator + sandbox
lifecycle, (c) rencana implementasi bertahap (fase 1: sandbox-per-subtask
dasar dengan Docker; fase 2: concurrency engine; fase 3: lifecycle
sleep/wake/recycle; fase 4: opsi isolasi microVM), (d) daftar file yang
perlu diubah/dibuat.
```

---

## Referensi gap analysis (untuk konteks, tidak perlu disertakan ke agent)

**Kondisi OpenCLAWN saat ini** (dari riset internal): sudah punya Docker sandbox dan multi-agent routing sebagai core architectural innovation, tapi belum ada bukti eksplisit soal isolasi per-subtask, orkestrasi paralel dengan dependency graph, atau lifecycle management sandbox (sleep/wake/recycle).

**Benchmark Manus**: VM/container dialokasikan per unit kerja, siklus create → sleep/awake (preserve state) → recycle setelah idle (7 hari free / 21 hari Pro), orkestrator memecah goal jadi subtask yang dieksekusi paralel oleh agent khusus. Underlying tech (dari repo reverse-engineered `whit3rabbit/manus-open`) ternyata Docker + FastAPI + WebSocket — bukan teknologi eksotis, artinya secara teknis achievable oleh proyek self-hosted dengan investasi engineering yang tepat.

**Prioritas yang disarankan** (bisa disisipkan ke sprint sequencing yang sudah ada, sprint 6-8):
1. Sandbox-per-subtask + DAG orchestrator (fondasi)
2. Concurrency engine + fault containment granular
3. Lifecycle management (sleep/wake/recycle)
4. Observability/replay per-subtask
5. Opsi upgrade isolasi ke gVisor/Firecracker (pluggable, bukan wajib)
