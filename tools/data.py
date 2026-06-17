"""Tool data: db_query (SELECT-only), memory_search, json_query.

db_query & memory_search memakai `DatabaseManager` (di-inject lewat `db=`).
json_query murni stdlib.
"""

import json

from tools.base import Tool

MAX_ROWS = 100
# Keyword yang menandakan operasi tulis/DDL — db_query menolaknya (read-only, §keamanan).
_FORBIDDEN = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "replace",
    "truncate",
    "attach",
    "pragma",
    "vacuum",
)
# Tabel yang boleh dibaca memory_search (introspeksi memori/skill, bukan kredensial).
_MEM_TABLES = {"memory_l1", "memory_l2", "skills"}


class DbQueryTool(Tool):
    """Jalankan SQL SELECT read-only ke DB internal. Tidak bisa menulis/DDL."""

    name = "db_query"
    requires_approval = True  # tetap minta approval — akses ke state internal

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        if db is None:
            return {"error": "db_query butuh koneksi database (tidak tersedia di konteks ini)"}
        sql = (input_data.get("sql") or "").strip().rstrip(";").strip()
        if not sql:
            return {"error": "sql wajib diisi"}

        lowered = sql.lower()
        # Hanya izinkan SELECT atau CTE (WITH ... SELECT). Read-only mutlak.
        if not (lowered.startswith("select") or lowered.startswith("with")):
            return {"error": "Hanya query SELECT yang diizinkan (read-only)"}
        # Cegah multi-statement & keyword tulis (defense in depth; execute() juga single-stmt).
        if ";" in sql:
            return {"error": "Hanya satu statement SELECT (tanpa ';')"}
        for kw in _FORBIDDEN:
            # cocokkan sebagai kata utuh agar 'created_at' tidak salah tolak.
            if f" {kw} " in f" {lowered} " or lowered.startswith(f"{kw} "):
                return {"error": f"Operasi '{kw}' tidak diizinkan — db_query read-only"}

        try:
            rows = await db.fetchall(sql)
        except Exception as e:  # error SQL (sintaks/tabel) — kembalikan, jangan crash
            return {"error": f"Query gagal: {e}"}
        return {"rows": rows[:MAX_ROWS], "count": len(rows), "truncated": len(rows) > MAX_ROWS}

    def schema(self) -> dict:
        return {
            "name": "db_query",
            "description": (
                "Jalankan query SQL SELECT read-only ke database internal agent "
                "(memori, skill, audit routing). Hanya SELECT — tidak bisa mengubah data. "
                "Tabel: memory_l1, memory_l2, skills, routing_events, role_handoffs, approval_log. "
                "SELALU butuh persetujuan user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "Query SELECT."}},
                "required": ["sql"],
            },
        }


class MemorySearchTool(Tool):
    """Cari di memori (L1/L2) & skill berdasarkan kata kunci. Read-only, no approval."""

    name = "memory_search"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        if db is None:
            return {"error": "memory_search butuh koneksi database"}
        query = (input_data.get("query") or "").strip()
        table = (input_data.get("table") or "skills").strip().lower()
        if not query:
            return {"error": "query wajib diisi"}
        if table not in _MEM_TABLES:
            return {"error": f"table harus salah satu dari {sorted(_MEM_TABLES)}"}

        # Kolom teks yang dicari per tabel (sesuai schema 001_initial.sql).
        col = {"memory_l1": "value", "memory_l2": "fact", "skills": "skill_content"}[table]
        like = f"%{query}%"
        try:
            rows = await db.fetchall(
                f"SELECT * FROM {table} WHERE {col} LIKE ? LIMIT ?",  # noqa: S608 — table dari allowlist
                (like, MAX_ROWS),
            )
        except Exception as e:
            return {"error": f"Pencarian gagal: {e}"}
        return {"table": table, "results": rows, "count": len(rows)}

    def schema(self) -> dict:
        return {
            "name": "memory_search",
            "description": (
                "Cari di memori agent (skills, memory_l1, memory_l2) berdasarkan kata kunci. "
                "Pakai untuk mengingat solusi/skill atau konteks sesi sebelumnya. Read-only."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Kata kunci yang dicari."},
                    "table": {
                        "type": "string",
                        "description": "skills (default), memory_l1, atau memory_l2.",
                    },
                },
                "required": ["query"],
            },
        }


class JsonQueryTool(Tool):
    """Ekstrak nilai dari JSON via dot-path (mis. 'data.items.0.name'). Stdlib."""

    name = "json_query"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        raw = input_data.get("json")
        path = (input_data.get("path") or "").strip()
        if raw is None:
            return {"error": "json wajib diisi (string atau object)"}
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as e:
            return {"error": f"JSON tidak valid: {e}"}

        if not path:
            return {"value": data}  # tanpa path → kembalikan seluruh struktur (sudah ter-parse)

        cur = data
        for key in path.split("."):
            try:
                if isinstance(cur, list):
                    cur = cur[int(key)]
                elif isinstance(cur, dict):
                    cur = cur[key]
                else:
                    return {"error": f"Tidak bisa menelusuri '{key}': bukan object/array"}
            except (KeyError, IndexError, ValueError):
                return {"error": f"Path '{path}' tidak ditemukan di '{key}'"}
        return {"value": cur}

    def schema(self) -> dict:
        return {
            "name": "json_query",
            "description": (
                "Ekstrak nilai dari data JSON memakai dot-path. "
                "Contoh path: 'results.0.title' atau 'user.name'. Path kosong = seluruh data. "
                "Pakai setelah http_request/file_read untuk mengambil field tertentu."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "json": {"description": "Data JSON (string atau object)."},
                    "path": {"type": "string", "description": "Dot-path, mis. 'a.b.0.c'."},
                },
                "required": ["json"],
            },
        }
