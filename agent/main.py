import argparse
import sys
from .loop import agent_loop, BudgetExceeded
from .worktree import (
    create_agent_worktree, commit_worktree,
    submit_and_merge, cleanup_worktree,
)


def run_task(task_name: str, task_description: str, auto_merge: bool = True) -> int:
    print(f"[1/5] 创建 worktree: agent/{task_name}")
    worktree_path, branch = create_agent_worktree(task_name)
    print(f"      -> {worktree_path}")

    exit_code = 0

    try:
        print(f"[2/5] 执行任务")
        result, usage = agent_loop(task_description, worktree_path)
        print(f"\n[Agent 总结]\n{result}\n")
        print(f"[用量] {usage.get('tokens', 0)} tokens, "
              f"{usage.get('duration', 0):.0f}s, "
              f"{usage.get('steps', 0)} steps")

        print(f"[3/5] 提交 worktree 改动")
        committed = commit_worktree(
            worktree_path, f"agent: {task_description[:60]}"
        )

        if not committed:
            print("      无改动可提交，跳过合并")
            exit_code = 0
        elif auto_merge:
            print(f"[4/5] 合并回主分支")
            merge_msg = submit_and_merge(worktree_path, branch)
            print(f"      {merge_msg}")
            exit_code = 2 if "冲突" in merge_msg else 0
        else:
            print(f"[4/5] 跳过合并（--no-merge），分支保留: {branch}")
            exit_code = 0

    except BudgetExceeded as e:
        print(f"\n[预算超限] {e}")
        exit_code = 3
    except KeyboardInterrupt:
        print("\n[中断] 用户取消")
        exit_code = 130
    except Exception as e:
        print(f"\n[错误] {e}")
        exit_code = 1

    finally:
        print(f"[5/5] 清理 worktree")
        if exit_code == 0 and auto_merge:
            cleanup_worktree(worktree_path, branch)
        else:
            print(f"      任务未成功或未自动合并，保留 worktree: {worktree_path}")
            print(f"      查看: cd {worktree_path} && git status && git diff")

    return exit_code


def main():
    parser = argparse.ArgumentParser(description="最小可运行的编码 Agent")
    parser.add_argument("task", help="任务描述")
    parser.add_argument("--name", default=None, help="任务短名（分支与目录用）")
    parser.add_argument("--no-merge", action="store_true", help="不自动合并")
    args = parser.parse_args()

    name = args.name or args.task[:30].replace(" ", "-").replace("/", "-")
    sys.exit(run_task(name, args.task, auto_merge=not args.no_merge))


if __name__ == "__main__":
    main()