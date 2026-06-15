from tools.base import Tool
from tools.file_ops import FileReadTool, FileWriteTool
from tools.web import WebFetchTool
from tools.interaction import AskUserTool
from tools.code import CodeRunTool

TOOL_REGISTRY: dict[str, Tool] = {
    "file_read": FileReadTool(),
    "file_write": FileWriteTool(),
    "web_fetch": WebFetchTool(),
    "ask_user": AskUserTool(),
    "code_run": CodeRunTool(),
}
