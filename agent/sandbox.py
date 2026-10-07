"""沙箱执行。优先用 bubblewrap 做 namespace 隔离，不可用时降级到本地 subprocess。"""

import os
import shutil
import subprocess
from .config import settings

_bwrap_path: str | None = None
_bwrap_checked = False


def _find_bwrap() -> str | None:
    global _bwrap_path, _bwrap_checked
    if not _bwrap_checked:
        _bwrap_path = shutil.which("bwrap")
        _bwrap_checked = True
        if _bwrap_path:
            print(f"[sandbox] 使用 bubblewrap: {_bwrap_path}")
        else:
            print("[sandbox] 未找到 bwrap，降级到本地 subprocess（无隔离）")
    return _bwrap_path


def execute_shell(command: str, workdir: str = ".", timeout: int = 30) -> str:
    if settings.sandbox_enabled == "off":
        return _local(command, workdir, timeout)

    bwrap = _find_bwrap()
    if bwrap:
        return _via_bwrap(bwrap, command, workdir, timeout)

    if settings.sandbox_enabled == "on":
        return "[沙箱错误] sandbox_enabled=on 但 bwrap 不可用"

    return _local(command, workdir, timeout)


def _via_bwrap(bwrap: str, command: str, workdir: str, timeout: int) -> str:
    workdir = os.path.abspath(workdir)

    # 系统目录只读挂载，让 shell、python、git 等能运行
    ro_binds = []
    for path in ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"]:
        if os.path.exists(path):
            ro_binds += ["--ro-bind", path, path]

    # 可选：挂载常用工具目录
    for path in ["/opt", "/usr/local"]:
        if os.path.exists(path):
            ro_binds += ["--ro-bind", path, path]

    # 临时目录（可写）
    tmp_binds = [
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
    ]

    # worktree 读写挂载
    work_bind = ["--bind", workdir, workdir]

    cmd = [
        bwrap,
        *ro_binds,
        *tmp_binds,
        *work_bind,
        "--chdir", workdir,
        "--unshare-net",          # 网络隔离
        "--unshare-pid",          # PID 隔离
        "--unshare-ipc",          # IPC 隔离
        "--unshare-uts",          # hostname 隔离
        "--die-with-parent",
        "--new-session",
        "bash", "-c", command,
    ]

    try:
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        out = r.stdout
        if r.stderr:
            out += f"\n[stderr]\n{r.stderr}"
        if r.returncode != 0:
            out += f"\n[exit code: {r.returncode}]"
        return out or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"[超时] 命令执行超过 {timeout}s"
    except Exception as e:
        return f"[沙箱错误] {e}"


def _local(command: str, workdir: str, timeout: int) -> str:
    """无隔离的本地执行，仅用于 sandbox_enabled=off 的测试场景。"""
    try:
        r = subprocess.run(
            command, shell=True, cwd=workdir,
            capture_output=True, text=True, timeout=timeout,
        )
        out = r.stdout
        if r.stderr:
            out += f"\n[stderr]\n{r.stderr}"
        if r.returncode != 0:
            out += f"\n[exit code: {r.returncode}]"
        return out or "(无输出)"
    except subprocess.TimeoutExpired:
        return f"[超时] 命令执行超过 {timeout}s"
    except Exception as e:
        return f"[错误] {e}"