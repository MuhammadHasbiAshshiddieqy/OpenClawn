from abc import ABC, abstractmethod


class Tool(ABC):
    name: str
    requires_approval: bool = False  # tool destruktif perlu approval
    # Timeout eksekusi khusus tool ini (detik). None → AppConfig.tool_timeout_sec.
    # Hanya untuk tool yang secara desain berjalan lama (mis. task_graph_submit).
    timeout_sec: int | None = None

    @abstractmethod
    async def execute(self, input_data: dict, vault, db=None) -> dict:
        """Jalankan tool.

        `vault`: ambil kredensial saat outbound (jangan masuk context).
        `db`: DatabaseManager untuk tool yang perlu DB (db_query, memory_search);
        tool lain mengabaikannya. Opsional agar test bisa memanggil tanpa DB.
        """
        ...

    @abstractmethod
    def schema(self) -> dict: ...
