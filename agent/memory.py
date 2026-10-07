"""跨任务记忆：任务完成后生成项目状态摘要，注入后续任务的 system prompt。"""
import json
import os
from pathlib import Path
from openai import OpenAI
from .config import settings
import re
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=settings.dashscope_base_url,
)

# 记忆文件统一放这里，避免和 worktree 的 cwd 绑定
# 记忆文件放持久挂载卷，容器重建不丢。
# 可用 AGENT_MEMORY_DIR 覆盖（测试时指到临时目录）。
MEMORY_DIR = Path(os.getenv(
    "AGENT_MEMORY_DIR",
    "/mnt/workspace/.mini-agent/memory",
))
MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _path(project_id: str) -> Path:
    return MEMORY_DIR / f"{project_id}.json"


def load_memory(project_id: str) -> dict:
    p = _path(project_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_memory(project_id: str, new_mem: dict) -> None:
    old = load_memory(project_id)
    merged = _merge(old, new_mem)
    _path(project_id).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _merge(old: dict, new: dict) -> dict:
    """按字段语义合并。"""
    out = dict(old)
    for k, v in new.items():
        new_list = v if isinstance(v, list) else []
        if k == "functions_defined":
            out[k] = _merge_functions(out.get(k, []), new_list)
        elif k == "conventions":
            out[k] = _merge_conventions(out.get(k, []), new_list)
        elif k == "recent_changes":
            out[k] = _concat_dedup(out.get(k, []), new_list)
        elif k == "files_created":
            out[k] = _concat_dedup(out.get(k, []), new_list)
        else:
            out[k] = v
    return out


def _merge_functions(old: list, new: list) -> list:
    """函数签名按函数名（'(' 前）覆盖，新值替换旧值。"""
    def key(fn: str) -> str:
        return fn.split("(")[0].strip()
    by_key: dict[str, str] = {}
    for fn in old:
        if isinstance(fn, str):
            by_key[key(fn)] = fn
    for fn in new:
        if isinstance(fn, str):
            by_key[key(fn)] = fn
    return list(by_key.values())


def _merge_conventions(old: list, new: list, max_items: int = 5) -> list:
    """保留最近的 max_items 条（后出现的优先），精确去重。"""
    combined = [x for x in (old + new) if isinstance(x, str) and x.strip()]
    seen: set[str] = set()
    result: list[str] = []
    for item in reversed(combined):       # 从新到旧遍历
        if item not in seen:
            seen.add(item)
            result.append(item)           # result 现在是「从新到旧」
    return result[:max_items]             # 直接取前 max_items = 最新的几条


def _concat_dedup(old: list, new: list) -> list:
    """拼接 + 精确字符串去重（用于 recent_changes / files_created）。"""
    seen = set()
    out = []
    for item in old + new:
        key = item if isinstance(item, str) else json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def summarize_task(task: dict, worktree_path: str, agent_summary: str) -> dict:
    """任务成功后，用 compress 模型把 agent 总结提取成结构化字段。

    用 agent 自己的总结而不是 git diff，省一次 LLM 调用和 diff 的 token。
    """
    prompt = f"""以下是一个编码任务的执行总结。请提取成 JSON：
- files_created: 本任务创建或修改的文件路径列表（字符串数组）
- functions_defined: 本任务新增的公开函数签名。格式必须是 `文件名.函数名(参数) -> 返回类型`，
  其中文件名必须来自 files_created 里的文件。不要编造模块名。
  例如文件是 "agents.py"、函数是 make_echo_agent，就写 "agents.make_echo_agent(role, goal) -> Agent"
- recent_changes: 一句话描述本任务做了什么（字符串）
- conventions: 本任务体现出的项目约定（如 "使用 pytest"），没有则空数组

任务名: {task['name']}
任务描述: {task['description']}
Agent 总结:
{agent_summary[:3000]}

只输出 JSON，字段缺失用空数组（recent_changes 用空字符串）。"""

    try:
        resp = client.chat.completions.create(
            model=settings.llm_model_compress,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[memory] summarize 失败: {e}")
        return {}

       # 归一化：recent_changes 统一成带任务名前缀的字符串列表
    # 归一化：recent_changes 统一成带任务名前缀的字符串列表
    change = data.get("recent_changes")
    if isinstance(change, str) and change.strip():
        data["recent_changes"] = [f"{task['name']}: {change.strip()}"]
    elif isinstance(change, list):
        data["recent_changes"] = [
            f"{task['name']}: {c}" for c in change if isinstance(c, str)
        ]
    else:
        data["recent_changes"] = []

    # 兜底：LLM 没给出有效 change 时，用 agent 总结第一行
    if not data["recent_changes"] and agent_summary.strip():
        fallback = agent_summary.strip().splitlines()[0][:120]
        data["recent_changes"] = [f"{task['name']}: {fallback}"]

    # 保证其他字段是 list
    for key in ("files_created", "functions_defined", "conventions"):
        if not isinstance(data.get(key), list):
            data[key] = []

    return data


def inject_memory_to_prompt(memory: dict, base_prompt: str = "") -> str:
    """把记忆拼成一段文本，供 agent_loop 的 extra_context 使用。
    限制注入量，避免每轮 LLM 调用都带过多历史。"""
    if not memory:
        return base_prompt

    parts = [base_prompt] if base_prompt else []
    parts.append("[项目上下文]")

    if memory.get("recent_changes"):
        parts.append("已完成的任务：")
        for change in memory["recent_changes"][-8:]:
            parts.append(f"- {change}")
    if memory.get("files_created"):
        files = memory["files_created"][-20:]
        parts.append(f"\n项目文件：{', '.join(files)}")
    if memory.get("functions_defined"):
        parts.append("已定义的函数（部分）：")
        for fn in memory["functions_defined"][-15:]:
            parts.append(f"- {fn}")
    if memory.get("conventions"):
        parts.append("约定：")
        for conv in memory["conventions"][:5]:
            parts.append(f"- {conv}")

    return "\n".join(parts)