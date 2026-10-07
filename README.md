# mini-agent

一个带安全护栏的编码 Agent。支持两种模式：**单任务 ReAct** 与 **项目级 Plan-and-Execute**。

## 它做什么

给它一个任务或一份 markdown spec，它会：在隔离的 Git worktree 中改代码 → 调用 LLM 工具 → 跑测试 → 自动合并回主分支。

## 架构

- **单任务模式**：`agent_loop` 是 ReAct 循环——LLM 每轮自主决定调哪个工具，直到不再调工具为止。
- **项目级模式**：`planner.py` 把 spec 拆成带依赖的任务图（SQLite 存储），`project.py` 按依赖调度执行。

## 核心设计

| 能力 | 实现 |
|---|---|
| 工具集 | 5 个：`shell_exec` / `read_file` / `write_file` / `search_code` / `ast_structure`（OpenAI Function Calling） |
| 隔离 | 每任务独立 Git worktree + 分支，失败保留现场，成功双重检查后清理 |
| 安全合并 | 文件锁串行化 + 主仓库脏检查 + 冲突诊断 |
| 跨任务记忆 | 按字段语义合并（函数签名按名覆盖、conventions 去重保最新），注入后续任务 system prompt |
| 失败处理 | 任务重试 3 次 → FAILED；依赖失败的任务 → BLOCKED；项目级暂停报告 |
| 安全护栏 | 危险命令正则审批、路径越界保护、多层预算控制（迭代/token/时长/项目累计/熔断） |
| 项目级验证 | `verify` 子命令：任务状态 + 仓库干净 + 无残留 worktree + 跑 pytest |

## 快速开始

```bash
pip install -e .
cp .env.example .env   # 填入 DASHSCOPE_API_KEY

# 单任务
export AGENT_REPO_ROOT=/path/to/your/repo
python -m agent.main "给 utils.py 加一个 add 函数并写测试" --name add-func

# 项目级
python -m agent.project plan spec.md --project myproj
python -m agent.project run myproj
python -m agent.project verify myproj
