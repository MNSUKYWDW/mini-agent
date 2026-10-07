import json
import os
import time
from openai import OpenAI
from .config import settings
from .tools import TOOL_DEFINITIONS, dispatch_tool
from dotenv import load_dotenv

load_dotenv()


class BudgetExceeded(Exception):
    def __init__(self, message: str, tokens: int = 0, duration: float = 0.0):
        super().__init__(message)
        self.tokens = tokens
        self.duration = duration


client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=settings.dashscope_base_url,
)


def _estimate_tokens(messages: list) -> int:
    total = 0
    for m in messages:
        total += len(str(m.get("content") or "")) // 4
        if m.get("tool_calls"):
            total += len(str(m["tool_calls"])) // 4
    return total


def _compress(messages: list, keep_recent: int = 4) -> list:
    if len(messages) <= keep_recent + 2:
        return messages
    system = messages[0]
    recent = messages[-keep_recent:]
    middle = messages[1:-keep_recent]

    summary = client.chat.completions.create(
        model=settings.llm_model_compress,
        messages=[
            {"role": "system",
             "content": "把以下对话压缩成简洁摘要，保留关键决策、文件路径、待办事项。"},
            *middle,
        ],
        max_tokens=800,
    ).choices[0].message.content

    return [system, {"role": "user", "content": f"[历史摘要]\n{summary}"}, *recent]


def agent_loop(
    user_task: str,
    workdir: str,
    extra_context: str = "",
) -> tuple[str, dict]:
    """执行单个任务。

    Args:
        user_task: 任务描述。
        workdir: 工作目录（worktree 路径）。
        extra_context: 额外注入 system prompt 的背景信息（如项目记忆）。

    Returns:
        (summary, usage)：summary 是 Agent 的最终回复文本；
        usage 含 tokens / duration / steps。
    """
    start_time = time.time()
    total_tokens = 0

    context_block = f"\n{extra_context}\n" if extra_context.strip() else ""
    system_prompt = f"""你是编码助手，在一个隔离的 Git worktree 中工作。

工作目录: {workdir}
所有命令必须在此目录内执行，不要切换到其他目录。
{context_block}
重要约束:
- 修改代码前先读懂上下文，不要盲目改动。
- 只读与当前任务直接相关的文件。如果记忆里提到了当前任务用不到的文件，不要为了「了解全貌」去读它。
- 只实现任务要求的功能。不要添加未被要求的辅助函数、额外 API、防御性代码或配置项。
- 测试数量与功能复杂度匹配。spec 说「至少 N 个」时，N+2 到 N+5 即可，不要为每个边界都写独立测试。
- 测试失败时，优先怀疑实现代码；只有确认测试本身写错时才修改测试。
- 修改完成后，重新运行测试验证。
- 遇到多个测试失败时，先判断它们是否共享同一根因，不要逐个修改。
- 不要重复执行相同的命令。如果之前的命令已经给出了答案，就基于它推进。
任务完成的判断标准：
1. 你实现了任务要求的所有改动
2. 你运行了验证命令（如 pytest）并确认通过
3. 改动已经过你的最后一遍检查，没有明显问题

满足以上三个条件后，立即输出最终总结并结束。
不要为了「更保险」反复运行同一个命令——重复验证不会提高结果质量。
完成任务后，用简洁的语言总结：改了什么、为什么改、验证结果。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_task},
    ]

    # 状态追踪（用于完成提示）
    wrote_code = False
    test_passed = False
    completion_hinted = False

    for step in range(settings.max_iterations):
        elapsed = time.time() - start_time

        # 1. 时长超限
        if elapsed > settings.max_duration_seconds:
            raise BudgetExceeded(
                f"超过最大执行时长 {settings.max_duration_seconds}s（已用 {elapsed:.0f}s）",
                tokens=total_tokens,
                duration=elapsed,
            )

        # 2. token 超限
        if total_tokens > settings.max_tokens:
            raise BudgetExceeded(
                f"超过最大 token 消耗 {settings.max_tokens}（已用 {total_tokens}）",
                tokens=total_tokens,
                duration=elapsed,
            )

        # 上下文压缩
        threshold = int(settings.context_limit * settings.compress_threshold)
        if _estimate_tokens(messages) > threshold:
            messages = _compress(messages)

        # 调用 LLM
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
        )

        # 累加 token
        if response.usage:
            total_tokens += response.usage.total_tokens

        msg = response.choices[0].message

        if not msg.tool_calls:
            duration = time.time() - start_time
            usage_info = {
                "tokens": total_tokens,
                "duration": duration,
                "steps": step + 1,
            }
            print(f"  [预算] 本次任务消耗 {total_tokens} tokens，耗时 {duration:.0f}s")
            return msg.content or "(无最终回复)", usage_info

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            print(f"  [step {step+1}] {name} id={tc.id[:8]} args={list(args.keys())}")
            result = dispatch_tool(name, args, workdir)

            # 检测「写过代码」
            if name == "write_file":
                wrote_code = True
            elif name == "shell_exec":
                cmd = args.get("command", "")
                if any(kw in cmd for kw in ["<<", "sed -i", "tee ", " > ", ">>"]):
                    wrote_code = True

            # 检测「测试通过」
            if name == "shell_exec" and "pytest" in args.get("command", ""):
                if "passed" in result and " failed" not in result.lower():
                    test_passed = True

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result[: settings.tool_output_limit],
            })

        # 完成提示：写过代码 + 测试通过 + 还没提示过
        if wrote_code and test_passed and not completion_hinted:
            messages.append({
                "role": "user",
                "content": (
                    "系统检测：你已经写过代码，并且最近一次 pytest 运行全部通过。"
                    "如果你认为任务已经完成，立即输出最终总结并结束。"
                    "如果还有关键收尾，只做必要步骤，不要重复验证。"
                ),
            })
            completion_hinted = True

    # 循环跑满 max_iterations，正常退出
    raise BudgetExceeded(
        f"达到最大迭代次数 {settings.max_iterations}",
        tokens=total_tokens,
        duration=time.time() - start_time,
    )