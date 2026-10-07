import json
import os
import sqlite3
import uuid
from datetime import datetime
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"   # 依赖失败，不可执行


DB_PATH = os.getenv("AGENT_DB_PATH", "/mnt/workspace/mini-agent/tasks.db")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                project_id TEXT,
                depends_on TEXT,
                retry_count INTEGER DEFAULT 0,
                result TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        # 兼容旧表：逐列 ALTER，已存在则忽略
        for col, decl in [
            ("project_id", "TEXT"),
            ("depends_on", "TEXT"),
            ("retry_count", "INTEGER DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass


def add_task(
    name: str,
    description: str,
    priority: int = 0,
    project_id: str | None = None,
    depends_on: list[str] | None = None,
) -> str:
    task_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO tasks "
            "(id, name, description, status, priority, project_id, depends_on, "
            " retry_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                task_id, name, description, TaskStatus.PENDING.value, priority,
                project_id, json.dumps(depends_on or []), now, now,
            ),
        )
    return task_id


def next_pending(project_id: str | None = None) -> dict | None:
    """取下一个可执行任务：PENDING 且所有依赖已 DONE。
    依赖失败的场景由 mark_blocked 提前处理。"""
    with _conn() as c:
        query = "SELECT * FROM tasks WHERE status = ?"
        params: list = [TaskStatus.PENDING.value]
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        query += " ORDER BY priority DESC, created_at ASC, rowid ASC"

        for row in c.execute(query, params).fetchall():
            task = dict(row)
            deps = json.loads(task.get("depends_on") or "[]")
            if not deps:
                return task
            placeholders = ",".join("?" * len(deps))
            done = c.execute(
                f"SELECT COUNT(*) FROM tasks "
                f"WHERE id IN ({placeholders}) AND status = ?",
                (*deps, TaskStatus.DONE.value),
            ).fetchone()[0]
            if done == len(deps):
                return task
    return None


def mark_blocked(project_id: str) -> int:
    """把 project 下所有依赖含 FAILED/BLOCKED 的 PENDING 任务标为 BLOCKED。
    返回本次标记数量。"""
    with _conn() as c:
        bad_ids = {
            r[0] for r in c.execute(
                "SELECT id FROM tasks WHERE status IN (?, ?)",
                (TaskStatus.FAILED.value, TaskStatus.BLOCKED.value),
            ).fetchall()
        }
        if not bad_ids:
            return 0
        count = 0
        now = datetime.now().isoformat()
        for row in c.execute(
            "SELECT id, depends_on FROM tasks WHERE status = ? AND project_id = ?",
            (TaskStatus.PENDING.value, project_id),
        ).fetchall():
            deps = json.loads(row[1] or "[]")
            if any(d in bad_ids for d in deps):
                c.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.BLOCKED.value, now, row[0]),
                )
                count += 1
        return count


def update_status(task_id: str, status: TaskStatus, result: str | None = None):
    now = datetime.now().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE tasks SET status = ?, result = ?, updated_at = ? WHERE id = ?",
            (status.value, result, now, task_id),
        )


def incr_retry(task_id: str) -> int:
    """retry_count + 1，返回新值。"""
    with _conn() as c:
        c.execute(
            "UPDATE tasks SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), task_id),
        )
        return c.execute(
            "SELECT retry_count FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]


def count_by_status(project_id: str, status: TaskStatus) -> int:
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status = ?",
            (project_id, status.value),
        ).fetchone()[0]


def get_task(task_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def all_tasks() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]