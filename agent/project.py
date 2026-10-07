"""项目协调器：规划 → 展示 → 确认 → 执行 → 暂停 → 报告。"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from .config import settings
from .queue import (
    init_db, add_task, next_pending, mark_blocked,
    count_by_status, TaskStatus, get_task,
)
from .runner import run_once
from .planner import plan_tasks, format_plan
from dotenv import load_dotenv

load_dotenv()
PROJECTS_DIR = Path(os.getenv(
    "AGENT_PROJECTS_DIR",
    "/mnt/workspace/.mini-agent/projects",
))
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# 项目级预算（所有任务累计）
MAX_PROJECT_TOKENS = int(os.getenv("AGENT_MAX_PROJECT_TOKENS", "800000"))
MAX_PROJECT_DURATION = int(os.getenv("AGENT_MAX_PROJECT_DURATION", "7200"))


def _runs_path(project_id: str) -> Path:
    return PROJECTS_DIR / f"{project_id}.runs.jsonl"


def _append_run(project_id: str, record: dict):
    """每个任务执行后追加一行 jsonl。"""
    with _runs_path(project_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_runs(project_id: str) -> list[dict]:
    p = _runs_path(project_id)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        # 非交互环境（比如管道）：默认不继续
        print("(非交互环境，视为否)")
        return False


def cmd_plan(spec_path: str, project_name: str, auto_yes: bool = False) -> int:
    """只规划，展示，写入队列。不执行。"""
    init_db()
    markdown = Path(spec_path).read_text(encoding="utf-8")
    print(f"[project] 读取 spec: {spec_path}（{len(markdown)} 字符）")
    print(f"[project] 规划中（模型: {settings.llm_model}）...")

    tasks, problems = plan_tasks(markdown, project_name)
    print()
    print(format_plan(tasks))

    if problems:
        print("\n[project] 规划存在问题：")
        for p in problems:
            print(f"  - {p}")
        if not auto_yes:
            print("\n[project] 拒绝写入队列。请修改 spec 或手动调整。")
            return 1
        if not _ask("仍要写入队列？(y/n): "):
            return 1

    if not auto_yes and not _ask(f"\n写入队列并开始执行？(y/n): "):
        print("[project] 已取消")
        return 0

    # 写队列（先写任务，再更新依赖映射——因为 add_task 返回的是新 id）
    # 这里用 planner 的 id 作为队列 id，保证依赖一致
    # 但 add_task 会生成新 id，所以改用直接插入
    from .queue import _conn
    now = datetime.now().isoformat()
    with _conn() as c:
        for t in tasks:
            c.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, name, description, status, priority, project_id, depends_on, "
                " retry_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    t.id, t.name, t.description, TaskStatus.PENDING.value,
                    t.priority, project_name, json.dumps(t.depends_on),
                    now, now,
                ),
            )
    print(f"[project] 已写入 {len(tasks)} 个任务到队列（project_id={project_name}）")
    return 0


def cmd_run(project_name: str, max_tasks: int | None = None) -> int:
    """执行队列中该项目的任务。每完成一个暂停。"""
    init_db()
    executed = 0
    total_tokens = 0
    start = time.time()

    while True:
        # 预算检查
        elapsed = time.time() - start
        if total_tokens > MAX_PROJECT_TOKENS:
            print(f"[project] 项目 token 预算超限（{total_tokens} > {MAX_PROJECT_TOKENS}），暂停")
            return 2
        if elapsed > MAX_PROJECT_DURATION:
            print(f"[project] 项目时长超限（{elapsed:.0f}s > {MAX_PROJECT_DURATION}s），暂停")
            return 2

        # 先处理阻塞：依赖失败的任务标 BLOCKED
        blocked = mark_blocked(project_name)
        if blocked:
            print(f"[project] {blocked} 个任务因依赖失败被阻塞")

        task = next_pending(project_name)
        if task is None:
            pending = count_by_status(project_name, TaskStatus.PENDING)
            running = count_by_status(project_name, TaskStatus.RUNNING)
            blocked_n = count_by_status(project_name, TaskStatus.BLOCKED)
            failed = count_by_status(project_name, TaskStatus.FAILED)
            done = count_by_status(project_name, TaskStatus.DONE)
            print(f"\n[project] 队列无可执行任务。统计：done={done} failed={failed} "
                  f"blocked={blocked_n} pending={pending} running={running}")
            if failed or blocked_n:
                _report_failures(project_name)
                return 1
            # 全部 done → 自动跑项目级验证
            return cmd_verify(project_name)

        print(f"\n{'='*60}")
        print(f"[project] 执行任务 {task['id']} - {task['name']}")
        print(f"{'='*60}")

        outcome = run_once(project_id=project_name)
        if outcome is None:
            # 理论不该发生（next_pending 刚返回任务）
            continue

        executed += 1
        tokens = outcome.get("usage", {}).get("tokens", 0)
        total_tokens += tokens

        _append_run(project_name, {
            "ts": datetime.now().isoformat(),
            "task_id": outcome["task_id"],
            "exit_code": outcome["exit_code"],
            "tokens": tokens,
            "duration": outcome.get("usage", {}).get("duration", 0),
            "steps": outcome.get("usage", {}).get("steps", 0),
        })

        print(f"\n[project] 任务 {outcome['task_id']} 结束，exit={outcome['exit_code']}，"
              f"tokens={tokens}，累计={total_tokens}")

        # 每完成一个暂停
        if max_tasks is not None and executed >= max_tasks:
            print(f"[project] 达到本次执行上限 {max_tasks}，暂停")
            return 0

        # 失败立即暂停（不自动继续，因为后续任务可能被阻塞）
        if outcome["exit_code"] != 0:
            print(f"\n[project] 任务失败（exit={outcome['exit_code']}），暂停")
            print(f"[project] 可检查 worktree 后重新运行 `python -m agent.project run {project_name}`")
            # 继续循环一次，让 mark_blocked 处理依赖
            blocked = mark_blocked(project_name)
            if blocked:
                print(f"[project] {blocked} 个后继任务被阻塞")
            _report_failures(project_name)
            return 1

        if not _ask("\n继续下一个任务？(y/n): "):
            print(f"[project] 已暂停。恢复: python -m agent.project run {project_name}")
            return 0


def _report_failures(project_name: str):
    """打印失败链。"""
    from .queue import _conn
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, status, result, depends_on FROM tasks "
            "WHERE project_id = ? AND status IN (?, ?)",
            (project_name, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value),
        ).fetchall()
    if not rows:
        return
    print("\n[project] 失败/阻塞任务：")
    for r in rows:
        print(f"  {r['id']}  {r['status']:8}  {r['name']}")
        if r["result"]:
            print(f"      结果: {r['result'][:200]}")


def cmd_status(project_name: str) -> int:
    """打印项目状态。"""
    init_db()
    from .queue import _conn
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, status, depends_on, retry_count, result "
            "FROM tasks WHERE project_id = ? ORDER BY created_at ASC",
            (project_name,),
        ).fetchall()
    if not rows:
        print(f"[project] 项目 {project_name} 无任务")
        return 0

    print(f"项目 {project_name}：")
    for r in rows:
        deps = json.loads(r["depends_on"] or "[]")
        dep_str = f" deps={deps}" if deps else ""
        retry = f" retry={r['retry_count']}" if r["retry_count"] else ""
        print(f"  {r['id']}  {r['status']:8}  {r['name']}{dep_str}{retry}")

    runs = _load_runs(project_name)
    if runs:
        total_tokens = sum(r.get("tokens", 0) for r in runs)
        print(f"\n执行记录：{len(runs)} 次，累计 {total_tokens} tokens")
    return 0

def _run_verify_command(repo: str, cmd: list[str]) -> tuple[int, str]:
    """在 repo 下跑验证命令，返回 (returncode, output)。"""
    import subprocess
    try:
        r = subprocess.run(
            cmd, cwd=repo, capture_output=True, text=True, timeout=300,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode, out
    except subprocess.TimeoutExpired:
        return 124, "(验证命令超时 300s)"
    except FileNotFoundError as e:
        return 127, f"(命令不存在: {e})"


def cmd_verify(project_name: str) -> int:
    """项目级最终验证。跑测试 + 状态一致性检查。返回 0 表示通过。"""
    init_db()
    from .config import settings
    from .queue import _conn

    repo = settings.repo_root
    print(f"[verify] 项目 {project_name}，仓库 {repo}")
    print()

    # ---- 1. 任务状态检查 ----
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) as n FROM tasks "
            "WHERE project_id = ? GROUP BY status",
            (project_name,),
        ).fetchall()
    status_counts = {r["status"]: r["n"] for r in rows}
    total = sum(status_counts.values())

    if total == 0:
        print("[verify] 项目无任务，无法验证")
        return 1

    print(f"[verify] 任务状态：{status_counts}")

    problems: list[str] = []

    # 有未完成的任务 → 不能验证
    unfinished = (
        status_counts.get(TaskStatus.PENDING.value, 0)
        + status_counts.get(TaskStatus.RUNNING.value, 0)
        + status_counts.get(TaskStatus.BLOCKED.value, 0)
    )
    if unfinished:
        problems.append(f"有 {unfinished} 个任务未完成（pending/running/blocked）")

    failed = status_counts.get(TaskStatus.FAILED.value, 0)
    if failed:
        problems.append(f"有 {failed} 个任务失败")

    done = status_counts.get(TaskStatus.DONE.value, 0)
    print(f"[verify] 完成 {done}/{total}")

    # ---- 2. 仓库状态检查 ----
    import subprocess
    raw_status = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    dirty_lines = [
        line for line in raw_status.splitlines()
        if "__pycache__" not in line
    ]
    dirty = "\n".join(dirty_lines).strip()
    if dirty:
        problems.append(f"仓库有未提交改动:\n{dirty[:300]}")
    else:
        print("[verify] 仓库工作区干净")

    # 检查残留 worktree（只查属于当前项目的）
    with _conn() as c:
        task_ids = {
            r[0] for r in c.execute(
                "SELECT id FROM tasks WHERE project_id = ?",
                (project_name,),
            ).fetchall()
        }
    wt_out = subprocess.run(
        ["git", "-C", repo, "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    agent_wts = []
    for line in wt_out.splitlines():
        if not line.startswith("worktree "):
            continue
        wt = line.split(" ", 1)[1]
        if "-agent-" in wt and any(wt.endswith(tid) for tid in task_ids):
            agent_wts.append(wt)
    if agent_wts:
        problems.append(f"有残留 worktree: {agent_wts}")
    else:
        print("[verify] 无残留 worktree")

    # ---- 3. 跑测试 ----
    # 约定：有 pytest 配置或 tests/ 目录就跑 pytest
    import os
    has_tests = (
        os.path.isdir(os.path.join(repo, "tests"))
        or os.path.exists(os.path.join(repo, "pytest.ini"))
        or os.path.exists(os.path.join(repo, "pyproject.toml"))
    )

    if has_tests:
        print("[verify] 运行 pytest...")
        code, out = _run_verify_command(repo, ["python", "-m", "pytest", "-q"])
        tail = "\n".join(out.strip().splitlines()[-5:])
        print(f"[verify] pytest 退出码 {code}")
        print(tail)
        if code != 0:
            problems.append(f"pytest 失败（退出码 {code}）")
    else:
        print("[verify] 未检测到 tests/ 或 pytest 配置，跳过测试")

    # ---- 4. 汇总 ----
    print()
    if problems:
        print("[verify] ❌ 验证失败：")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("[verify] ✅ 项目验证通过")
    return 0

def main():
    parser = argparse.ArgumentParser(description="项目级 Agent 协调器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="规划并写入队列")
    p_plan.add_argument("spec", help="markdown spec 路径")
    p_plan.add_argument("--project", required=True, help="项目名（project_id）")
    p_plan.add_argument("--yes", action="store_true", help="跳过确认")

    p_run = sub.add_parser("run", help="执行队列中的项目任务")
    p_run.add_argument("project")
    p_run.add_argument("--max-tasks", type=int, default=None,
                       help="本次最多执行几个任务（默认无限）")

    p_status = sub.add_parser("status", help="查看项目状态")
    p_status.add_argument("project")

    p_verify = sub.add_parser("verify", help="项目级最终验证")     # 新增
    p_verify.add_argument("project")                              # 新增

    args = parser.parse_args()

    if args.cmd == "plan":
        sys.exit(cmd_plan(args.spec, args.project, auto_yes=args.yes))
    elif args.cmd == "run":
        sys.exit(cmd_run(args.project, max_tasks=args.max_tasks))
    elif args.cmd == "status":
        sys.exit(cmd_status(args.project))
    elif args.cmd == "verify":                                    # 新增
        sys.exit(cmd_verify(args.project))                        # 新增


if __name__ == "__main__":
    main()