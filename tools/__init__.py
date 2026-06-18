from tools.base import Tool
from tools.file_ops import (
    ApplyPatchTool,
    FileAppendTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    ReadManyTool,
)
from tools.web import HttpRequestTool, WebFetchTool, WebSearchTool
from tools.interaction import AskUserTool
from tools.code import CodeRunTool
from tools.shell import ListDirTool, ShellRunTool
from tools.search import GlobTool, GrepTool
from tools.document import DocWriteTool, PdfReadTool
from tools.data import DbQueryTool, JsonQueryTool, MemorySearchTool

TOOL_REGISTRY: dict[str, Tool] = {
    # filesystem (workspace-bounded)
    "file_read": FileReadTool(),
    "read_many": ReadManyTool(),
    "file_write": FileWriteTool(),
    "file_edit": FileEditTool(),
    "file_append": FileAppendTool(),
    "apply_patch": ApplyPatchTool(),
    "list_dir": ListDirTool(),
    "glob": GlobTool(),
    "grep": GrepTool(),
    "pdf_read": PdfReadTool(),
    "doc_write": DocWriteTool(),
    # eksekusi (sandboxed)
    "shell_run": ShellRunTool(),
    "code_run": CodeRunTool(),
    # akses luar
    "web_fetch": WebFetchTool(),
    "web_search": WebSearchTool(),
    "http_request": HttpRequestTool(),
    # data & memori
    "db_query": DbQueryTool(),
    "memory_search": MemorySearchTool(),
    "json_query": JsonQueryTool(),
    # interaksi
    "ask_user": AskUserTool(),
}
