import time
from .queue import (
    init_db, next_pending, update_status, incr_retry, TaskStatus,
)
from .loop import agent_loop, BudgetExceeded
from .worktree import (
    create_agent_worktree, commit_worktree,
    submit_and_merge, cleanup_worktree,
)

MAX_CONSECUTIVE_FAILURES = 3
MAX_RETRIES = 3


def execute_task(task: dict) -> dict:
    """执行单个任务。返回 {'exit_code': int, 'summary': str, 'usage': dict}。"""
    task_id = task["id"]
    name = task["name"]
    description = task["description"]
    project_id = task.get("project_id")

    print(f"\n[runner] 任务 {task_id} - {name}")
    # worktree 用 task_id 命名，避免同名任务撞分支
    worktree_path, branch = create_agent_worktree(task_id)

    exit_code = 0
    summary = ""
    usage: dict = {}

    try:
        # 注入项目记忆（无 project_id 时为空）
        extra = ""
        if project_id:
            try:
                from .memory import load_memory, inject_memory_to_prompt
                extra = inject_memory_to_prompt(load_memory(project_id))
            except Exception as e:
                print(f"[runner] 加载记忆失败（继续执行）: {e}")

        result, usage = agent_loop(description, worktree_path, extra_context=extra)
        summary = result
        print(f"\n[Agent 总结]\n{result}\n")

        committed = commit_worktree(worktree_path, f"agent: {name[:60]}")
        if not committed:
            print("[runner] 无改动，跳过合并")
            exit_code = 0
        else:
            merge_msg = submit_and_merge(worktree_path, branch)
            print(f"[runner] {merge_msg}")
            exit_code = 2 if "冲突" in merge_msg else 0

    except BudgetExceeded as e:
        print(f"\n[runner] 预算超限: {e}")
        summary = f"预算超限: {e}"
        exit_code = 3
        usage = {"tokens": e.tokens, "duration": e.duration, "steps": 0}
    except Exception as e:
        print(f"[runner] 任务异常: {e}")
        summary = f"异常: {e}"
        exit_code = 1

    finally:
        if exit_code == 0:
            cleanup_worktree(worktree_path, branch)
        else:
            print(f"[runner] 任务未成功（exit {exit_code}），保留 worktree 供检查")
            print(f"[runner]   cd {worktree_path} && git status && git diff")

    # 任务成功且属项目：生成记忆（失败不影响任务结果）
    if exit_code == 0 and project_id and summary:
        try:
            from .memory import summarize_task, save_memory
            new_mem = summarize_task(task, worktree_path, summary)
            if new_mem:
                save_memory(project_id, new_mem)
                print(f"[runner] 记忆已更新: {project_id}")
        except Exception as e:
            print(f"[runner] 生成记忆失败（不影响任务）: {e}")

    return {"exit_code": exit_code, "summary": summary, "usage": usage}


def run_once(project_id: str | None = None) -> dict | None:
    """执行一个任务。返回 {'task_id', 'exit_code', 'usage', 'retried'}。
    队列为空返回 None。"""
    task = next_pending(project_id)
    if not task:
        return None

    update_status(task["id"], TaskStatus.RUNNING)
    outcome = execute_task(task)
    code = outcome["exit_code"]

    if code == 0:
        update_status(
            task["id"], TaskStatus.DONE,
            (outcome["summary"] or "成功")[:500],
        )
    else:
        retries = incr_retry(task["id"])
        if retries < MAX_RETRIES:
            update_status(
                task["id"], TaskStatus.PENDING,
                f"第 {retries} 次失败，退出码 {code}",
            )
            print(f"[runner] 任务 {task['id']} 第 {retries} 次失败，退回队列")
        else:
            update_status(
                task["id"], TaskStatus.FAILED,
                f"重试 {retries} 次仍失败，退出码 {code}",
            )

    return {
        "task_id": task["id"],
        "exit_code": code,
        "usage": outcome.get("usage", {}),
        "retried": code != 0,
    }


def run_forever(poll_interval: int = 5):
    init_db()
    failures = 0

    while True:
        if failures >= MAX_CONSECUTIVE_FAILURES:
            print(f"[runner] 连续失败 {failures} 次，暂停等待人工检查")
            return

        outcome = run_once()
        if outcome is None:
            print(f"[runner] 队列空，{poll_interval}s 后重试（Ctrl+C 退出）")
            time.sleep(poll_interval)
            failures = 0
            continue

        if outcome["exit_code"] == 0:
            failures = 0
        else:
            failures += 1