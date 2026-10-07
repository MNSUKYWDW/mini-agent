import os
import ast
import re
import json
from pydantic import BaseModel, Field
from .sandbox import execute_shell


# ---------- 参数模型 ----------

class ShellExecParams(BaseModel):
    command: str = Field(description="要执行的 shell 命令")
    timeout: int = Field(default=30, description="超时秒数")

class ReadFileParams(BaseModel):
    path: str = Field(description="相对于工作目录的文件路径")

class WriteFileParams(BaseModel):
    path: str = Field(description="相对于工作目录的文件路径")
    content: str = Field(description="要写入的完整内容")

class SearchCodeParams(BaseModel):
    pattern: str = Field(description="正则模式")
    directory: str = Field(default=".", description="相对于工作目录的搜索目录")

class ASTStructureParams(BaseModel):
    file_path: str = Field(description="相对于工作目录的文件路径")


# ---------- 工具 schema ----------

TOOL_DEFINITIONS = [
    {"type": "function", "function": {
        "name": "shell_exec",
        "description": "执行 shell 命令",
        "parameters": ShellExecParams.model_json_schema(),
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取文件，返回带行号的文本",
        "parameters": ReadFileParams.model_json_schema(),
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "写入文件，覆盖原内容",
        "parameters": WriteFileParams.model_json_schema(),
    }},
    {"type": "function", "function": {
        "name": "search_code",
        "description": "在目录中按正则搜索代码",
        "parameters": SearchCodeParams.model_json_schema(),
    }},
    {"type": "function", "function": {
        "name": "ast_structure",
        "description": "获取 Python 文件的结构大纲",
        "parameters": ASTStructureParams.model_json_schema(),
    }},
]


# ---------- 审批 ----------

DANGEROUS_PATTERNS = [
    (r"\brm\s+-rf\b", "rm -rf"),
    (r"\brm\s+-r\s+/\b", "rm -r /"),
    (r"\bgit\s+push\b", "git push"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+clean\s+-fd\b", "git clean -fd"),
    (r"\bDROP\s+(TABLE|DATABASE)\b", "DROP TABLE/DATABASE"),
    (r"\bchmod\s+(-R\s+)?777\b", "chmod 777"),
    (r"(?<!\w)sudo\s", "sudo"),
    (r"(?<!\w)su\s+-", "su -"),
    (r"(?<!\w)curl\s+http", "curl"),
    (r"(?<!\w)wget\s+http", "wget"),
    (r"(?<!\w)nc\s+-", "nc"),
    (r"(?<!\w)netcat\s", "netcat"),
    (r">\s*/dev/sd", "write to block device"),
    (r"(?<!\w)mkfs\b", "mkfs"),
    (r"(?<!\w)dd\s+if=", "dd if="),
    (r"\bmv\s+/\*\s", "mv /* "),
    (r"\bfind\s+/\s+-delete\b", "find / -delete"),
]

_APPROVED: set[str] = set()


def _needs_approval(command: str) -> tuple[bool, str]:
    for pattern, label in DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return True, label
    return False, ""


# ---------- 路径保护 ----------

def _resolve(base_dir: str, path: str) -> str:
    base_abs = os.path.abspath(base_dir)
    if os.path.isabs(path):
        full = os.path.abspath(path)
    else:
        full = os.path.abspath(os.path.join(base_dir, path))

    if not full.startswith(base_abs + os.sep) and full != base_abs:
        raise ValueError(f"路径越界: {path} 不在 {base_dir} 内")
    return full


# ---------- 工具实现 ----------

def _read_file(base_dir: str, params: ReadFileParams) -> str:
    full = _resolve(base_dir, params.path)
    if not os.path.exists(full):
        return f"[错误] 文件不存在: {params.path}"
    try:
        with open(full, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"[错误] {e}"
    return "".join(f"{i+1:>4} | {l}" for i, l in enumerate(lines)) or "(空文件)"


def _write_file(base_dir: str, params: WriteFileParams) -> str:
    full = _resolve(base_dir, params.path)
    try:
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(params.content)
        return f"已写入 {params.path}（{len(params.content)} 字符）"
    except Exception as e:
        return f"[错误] {e}"


def _search_code(base_dir: str, params: SearchCodeParams) -> str:
    try:
        pattern = re.compile(params.pattern)
    except re.error as e:
        return f"[错误] 无效正则: {e}"

    target = _resolve(base_dir, params.directory)
    skip = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
    results = []

    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            fp = os.path.join(root, name)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            rel = os.path.relpath(fp, base_dir)
                            results.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(results) >= 100:
                                return "\n".join(results) + "\n...(已截断)"
            except (UnicodeDecodeError, PermissionError):
                continue
    return "\n".join(results) or "(无匹配)"


def _ast_structure(base_dir: str, params: ASTStructureParams) -> str:
    full = _resolve(base_dir, params.file_path)
    if not os.path.exists(full):
        return f"[错误] 文件不存在: {params.file_path}"
    try:
        with open(full, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except SyntaxError as e:
        return f"[错误] 语法错误: {e}"
    except Exception as e:
        return f"[错误] {e}"

    lines = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                lines.append(f"L{node.lineno}: import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            lines.append(f"L{node.lineno}: from {node.module or ''} import ...")
        elif isinstance(node, ast.ClassDef):
            lines.append(f"L{node.lineno}: class {node.name}")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append(f"  L{item.lineno}: def {item.name}()")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(f"L{node.lineno}: def {node.name}()")
    return "\n".join(lines) or "(无结构信息)"


# ---------- 分发 ----------

def dispatch_tool(name: str, args: dict, base_dir: str) -> str:
    try:
        if name == "shell_exec":
            p = ShellExecParams(**args)

            risky, matched = _needs_approval(p.command)
            if risky and matched not in _APPROVED:
                print(f"\n[需要审批] 命中危险模式: {matched}")
                print(f"[命令] {p.command}")
                ans = input("执行？(y=允许一次 / a=本会话允许此模式 / n=拒绝): ").strip().lower()
                if ans == "a":
                    _APPROVED.add(matched)
                elif ans != "y":
                    return "用户拒绝执行此命令。"

            return execute_shell(p.command, workdir=base_dir, timeout=p.timeout)

        elif name == "read_file":
            return _read_file(base_dir, ReadFileParams(**args))
        elif name == "write_file":
            return _write_file(base_dir, WriteFileParams(**args))
        elif name == "search_code":
            return _search_code(base_dir, SearchCodeParams(**args))
        elif name == "ast_structure":
            return _ast_structure(base_dir, ASTStructureParams(**args))
        return f"[错误] 未知工具: {name}"
    except Exception as e:
        return f"[错误] 工具执行异常: {e}"