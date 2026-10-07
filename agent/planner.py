"""规划器：把 markdown 项目需求拆成任务列表。"""
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from openai import OpenAI
from .config import settings
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=settings.dashscope_base_url,
)

MAX_TASKS = 15


@dataclass
class PlannedTask:
    id: str
    name: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    priority: int = 0
    verify_hint: str = ""


PLAN_SYSTEM_PROMPT = """你是项目规划助手。你的职责是把一份项目需求拆成多个**小而明确**的子任务。

每个任务必须满足：
- 只涉及 1-2 个文件的改动
- 有明确的验证方式（通常是 pytest，或一条具体的 shell 命令）
- 描述清楚，包含足够上下文，执行者不需要额外信息就能做
- 能在 10 步工具调用内完成

不要把任务拆成「设计架构」「调研方案」这类无验证标准的工作。
每个任务应该产出可运行、可测试的代码。

依赖关系用**数组下标**表示，从 0 开始。
例如：任务 3 依赖任务 1，写 "depends_on": [0]。
如果任务不依赖任何前置，写 "depends_on": []。

输出 JSON，格式：
{
  "tasks": [
    {
      "name": "简短名称（10 字内）",
      "description": "给执行者的完整指令，说明改什么文件、实现什么、怎么验证",
      "depends_on": [0],
      "priority": 0,
      "verify_hint": "怎么验证成功，如 'pytest tests/test_x.py 全绿'"
    }
  ]
}
"""


def _call_llm(markdown: str, project_name: str) -> dict:
    prompt = f"""项目名：{project_name}

项目需求（markdown）：
---
{markdown}
---

请把以上需求拆成子任务，输出 JSON。"""

    resp = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"规划器返回非法 JSON: {e}\n原始输出:\n{content[:500]}")


def _detect_cycle(tasks: list[PlannedTask]) -> list[str]:
    """检测依赖环，返回问题描述列表。"""
    id_map = {t.id: t for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {t.id: WHITE for t in tasks}
    problems = []

    def dfs(tid: str, path: list[str]):
        if color[tid] == GRAY:
            cycle = path[path.index(tid):] + [tid]
            problems.append(f"依赖环: {' -> '.join(cycle)}")
            return
        if color[tid] == BLACK:
            return
        color[tid] = GRAY
        for dep in id_map[tid].depends_on:
            if dep in id_map:
                dfs(dep, path + [tid])
        color[tid] = BLACK

    for t in tasks:
        if color[t.id] == WHITE:
            dfs(t.id, [])
    return problems


def validate_plan(tasks: list[PlannedTask]) -> list[str]:
    """返回问题列表，空表示通过。"""
    problems: list[str] = []
    if not tasks:
        return ["规划结果为空"]

    if len(tasks) > MAX_TASKS:
        problems.append(f"任务数 {len(tasks)} 超过 {MAX_TASKS}，可能拆得太粗")

    valid_ids = {t.id for t in tasks}
    for i, t in enumerate(tasks):
        if not t.name.strip():
            problems.append(f"任务 {i} 缺少 name")
        if len(t.description) < 20:
            problems.append(f"任务 {i} ({t.name}) description 过短（{len(t.description)} 字符），可能不明确")
        if len(t.description) > 800:
            problems.append(f"任务 {i} ({t.name}) description 过长（{len(t.description)} 字符），可能拆得不够细")
        for d in t.depends_on:
            if d not in valid_ids:
                problems.append(f"任务 {i} ({t.name}) 依赖不存在的 id: {d}")

    problems.extend(_detect_cycle(tasks))
    return problems


def plan_tasks(markdown: str, project_name: str) -> tuple[list[PlannedTask], list[str]]:
    """返回 (tasks, problems)。problems 非空时 tasks 仍可用于展示，但不应写入队列。"""
    raw = _call_llm(markdown, project_name)
    raw_tasks = raw.get("tasks", [])
    if not isinstance(raw_tasks, list):
        return [], ["LLM 输出中 tasks 不是数组"]

    # 生成 id，按下标映射依赖
    ids = [uuid.uuid4().hex[:8] for _ in raw_tasks]
    tasks: list[PlannedTask] = []
    for i, rt in enumerate(raw_tasks):
        if not isinstance(rt, dict):
            continue
        dep_indices = rt.get("depends_on", [])
        if not isinstance(dep_indices, list):
            dep_indices = []
        dep_ids = [ids[j] for j in dep_indices if isinstance(j, int) and 0 <= j < len(ids)]
        tasks.append(PlannedTask(
            id=ids[i],
            name=str(rt.get("name", f"task-{i}")).strip(),
            description=str(rt.get("description", "")).strip(),
            depends_on=dep_ids,
            priority=int(rt.get("priority", 0)) if isinstance(rt.get("priority"), int) else 0,
            verify_hint=str(rt.get("verify_hint", "")).strip(),
        ))

    problems = validate_plan(tasks)
    return tasks, problems


def format_plan(tasks: list[PlannedTask]) -> str:
    """把任务列表格式化成可读文本。"""
    lines = [f"规划了 {len(tasks)} 个任务："]
    id_to_name = {t.id: t.name for t in tasks}
    for i, t in enumerate(tasks):
        deps = ""
        if t.depends_on:
            dep_names = [id_to_name.get(d, d) for d in t.depends_on]
            deps = f"  [依赖: {', '.join(dep_names)}]"
        hint = f"  (验证: {t.verify_hint})" if t.verify_hint else ""
        lines.append(f"  [{i}] {t.id}  {t.name}{deps}{hint}")
    return "\n".join(lines)