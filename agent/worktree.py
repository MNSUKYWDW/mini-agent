import os
import re
import subprocess
import fcntl
from .config import settings


def _git(repo: str, *args: str, check: bool = False):
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=check,
    )


def current_branch(repo: str) -> str:
    result = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        raise RuntimeError(
            f"无法确定 {repo} 的当前分支。"
            f"请确保它是一个 git 仓库且不在 detached HEAD 状态。"
        )
    return branch


def _sanitize_task_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:40] or "task"


def create_agent_worktree(task_name: str) -> tuple[str, str]:
    task_name = _sanitize_task_name(task_name)
    repo = settings.repo_root
    branch = f"agent/{task_name}"
    worktree_path = os.path.join(
        os.path.dirname(repo),
        f"{os.path.basename(repo)}-agent-{task_name}",
    )

    _git(repo, "worktree", "remove", worktree_path, "--force")
    _git(repo, "branch", "-D", branch)

    base = current_branch(repo)
    _git(repo, "branch", branch, base, check=True)
    _git(repo, "worktree", "add", worktree_path, branch, check=True)
    return worktree_path, branch


def commit_worktree(worktree_path: str, message: str) -> bool:
    import shutil

    # 从 git 索引和磁盘移除临时产物
    junk_patterns = ["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]
    for junk in junk_patterns:
        # 从索引移除（如果被跟踪）
        subprocess.run(
            ["git", "-C", worktree_path, "rm", "-r", "--cached", "--ignore-unmatch", junk],
            capture_output=True,
        )
        # 从磁盘移除
        for root, dirs, _ in os.walk(worktree_path):
            if junk in dirs:
                shutil.rmtree(os.path.join(root, junk), ignore_errors=True)

    _git(worktree_path, "add", "-A")

    diff = subprocess.run(
        ["git", "-C", worktree_path, "diff", "--cached", "--quiet"],
    )
    if diff.returncode == 0:
        return False

    r = _git(worktree_path, "commit", "-m", message, "--no-verify")
    return r.returncode == 0


def submit_and_merge(worktree_path: str, branch: str) -> str:
    import shutil

    repo = settings.repo_root
    target = settings.target_branch or current_branch(repo)

    # 清理主仓库的临时产物
    junk_patterns = ["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]
    subprocess.run(
        ["git", "-C", repo, "rm", "-r", "--cached", "--ignore-unmatch",
         *junk_patterns],
        capture_output=True,
    )
    for junk in junk_patterns:
        for root, dirs, _ in os.walk(repo):
            if ".git" in root.split(os.sep):
                continue
            if junk in dirs:
                shutil.rmtree(os.path.join(root, junk), ignore_errors=True)

    # 检查主仓库是否有未提交改动
    dirty = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout.strip()

    if dirty:
        return (
            f"主仓库有未提交改动，无法合并。请先处理：\n"
            f"{dirty[:500]}"
        )

    # 加锁合并
    lock_path = os.path.join(repo, ".git", "agent-merge.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            count = _git(repo, "rev-list", "--count",
                         f"{target}..{branch}").stdout.strip()
            if count == "0":
                return "无新提交，跳过合并"

            result = _git(repo, "merge", branch, "--no-edit")

            if result.returncode != 0:
                err = (result.stderr or result.stdout).strip()
                _git(repo, "merge", "--abort")
                conflicts = _git(repo, "diff", "--name-only",
                                 "--diff-filter=U").stdout.strip()
                return (
                    f"合并失败。\n"
                    f"[git 输出]\n{err}\n"
                    f"[冲突文件]\n{conflicts or '(无内容冲突)'}"
                )

            return f"合并成功: {branch} -> {target}"
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def cleanup_worktree(worktree_path: str, branch: str, force: bool = False):
    if not os.path.exists(worktree_path):
        return

    repo = settings.repo_root
    target = settings.target_branch or current_branch(repo)

    if force:
        r = subprocess.run(
            ["git", "-C", repo, "worktree", "remove", worktree_path, "--force"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"[清理] worktree remove 失败: {r.stderr.strip()}")
        r = subprocess.run(
            ["git", "-C", repo, "branch", "-D", branch],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"[清理] branch -D 失败: {r.stderr.strip()}")
        return

    # 检查 worktree 工作区是否干净
    dirty = subprocess.run(
        ["git", "-C", worktree_path, "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout.strip()

    if dirty:
        print(f"[清理] {worktree_path} 有未提交改动，保留 worktree")
        print(f"[清理] 查看改动: cd {worktree_path} && git status")
        return

    # 检查分支是否已合并（兼容 ` `、`*`、`+` 三种前缀）
    merged = subprocess.run(
        ["git", "-C", repo, "branch", "--merged", target],
        capture_output=True, text=True,
    ).stdout

    patterns = [" " + branch + "\n", "* " + branch + "\n", "+ " + branch + "\n"]
    if not any(p in merged for p in patterns):
        print(f"[清理] {branch} 未合并到 {target}，保留 worktree")
        return

    # 执行清理，检查每步结果
    r = subprocess.run(
        ["git", "-C", repo, "worktree", "remove", worktree_path, "--force"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[清理] worktree remove 失败: {r.stderr.strip()}")
        return

    r = subprocess.run(
        ["git", "-C", repo, "branch", "-D", branch],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[清理] branch -D 失败: {r.stderr.strip()}")
        return

    print(f"[清理] 已清理 {branch}")