# Cyan

一个命令行编程智能体：接到自然语言任务后，自主读取目录、读写文件、执行命令，直到任务完成。

不依赖任何 Agent 框架或 SDK（LangChain / LlamaIndex / OpenAI Agents SDK 等一概未用），
Agent Loop、工具系统、模型输出解析、安全策略、终止条件与错误恢复全部自行实现，
仅使用模型厂商的 OpenAI 兼容 API 与原生 Tool Calling 接口。

## 快速开始

```bash
uv sync
echo "DEEPSEEK_API_KEY=sk-..." > .env

# 交互 REPL
uv run cyan
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-w, --workspace` | 工作目录，默认当前目录；Agent 只能访问该目录内的文件 |
| `-m, --model` | 模型名称，默认 `deepseek-chat` |
| `-c, --continue` | 恢复本工作区最近一次会话（`~/.cyan/projects/.../last`） |
| `--resume` | 列出或指定会话 id 恢复 |
| `--max-iterations` | 单任务最大轮次，默认 30 |
| `--verbose` | 额外把日志打到 stderr（默认只写文件，不干扰 rich 界面） |
| `--no-stream` | 关闭流式输出，改为等模型说完整段话再一次性显示（也可用环境变量 `CYAN_STREAM=0`） |

终端界面用 rich 渲染（Markdown、diff、审批面板）。助手回复默认按 SSE 流式打字机效果实时显示，`--no-stream` 可关闭退化成一次性显示。运行日志写入工作目录的 `.cyan/logs/agent.log`。会话事件日志在用户主目录 `~/.cyan/projects/<路径编码>/<session-id>.jsonl`（可用 `CYAN_HOME` 覆盖），详见 [docs/session-store.md](docs/session-store.md)。

交互模式下可用 `/help`、`/tools`、`/usage`、`/stream`、`/instructions`、`/memory`、`/compact`、`/loop`、`/context`、`/model`、`/status`、`/todos`、`/changes`、`/history`、`/rewind`、`/sessions`、`/resume`、`/new`、`/cwd`、`/exit`，任务执行中按 Ctrl-C 中断。`/compact`、`/loop`、`/tools`、`/context` 支持不带参数查看当前值，或 `<字段> <值>` 修改本会话的运行时策略（不影响下次启动的默认值）；`/model` 查看或切换模型；`/status` 一屏汇总模型、权限模式、流式开关、上下文占用与调用统计；`/todos` 查看模型用 `todo_write` 维护的当前任务清单，`/todos clear` 手动清空；`/changes` 列出本次会话里被 `write_file`/`edit_file` 改动过的文件；`/resume [<id 或前缀>]`（别名 `/continue`）在 REPL 内部直接切到另一个已保存的会话，不带参数列出可选会话，切换后沿用当前会话的权限模式。全部命令的详细用法、参数与可改字段见 [docs/commands.md](docs/commands.md)。

输入框基于 `prompt_toolkit`：敲 `/` 后不用按 Tab，随打字自动弹出、实时过滤的命令候选列表（方向键选择、Enter/Tab 确认），历史记录持久化在 `<home>/history`（`home` 同会话存储根，`CYAN_HOME` 可覆盖）跨会话保留。等待模型返回首个响应片段期间会显示一个简单的转圈动画，一旦文本开始流式输出即消失。

## 指令层（cyan.md）

开发者维护的持久化规则，组窗时作为独立 Prompt Layer 叠进 system 角色，**不写入会话日志**。从宽到窄：

| 位置 | 作用 |
| --- | --- |
| `~/.cyan/cyan.md` | 个人偏好（所有项目）；`CYAN_HOME` 可覆盖根目录 |
| `{workspace}/.cyan/cyan.md` | 本仓库的团队规范，可提交进 git |
| `{workspace}/cyan.md` | 仅当 `.cyan/cyan.md` 不存在时的过渡位置 |

缺文件则跳过。改完下一轮模型调用即生效。用 `/instructions` 查看当前加载了哪些层。

## 自动记忆

项目级笔记写在 `{workspace}/.cyan/memory/`（git 忽略，不共享）：

| 文件 | 作用 |
| --- | --- |
| `MEMORY.md` | 索引，每条一行，组窗时加载 |
| `user.md` | 协作者偏好、角色（按需 `memory_read`） |
| `feedback.md` | 纠错、被确认的做法 |
| `project.md` | 代码 / git 看不出的进度与决策 |
| `reference.md` | 仓库外入口 |

任务中可用 `memory_write` 即时写入；任务 **成功结束** 后会再提取一次。中断或失败不沉淀。`CYAN_DISABLE_AUTO_MEMORY=1` 可关闭。用 `/memory` 查看文件。cyan.md 是人写的规则，memory 是 Agent 写的笔记。

## 任务规划

多步骤任务由模型自己调用 `todo_write` 维护一份结构化清单（对齐 Claude Code 的 TodoWrite）：每次调用传入
**完整**清单（覆盖式更新，不是增量 patch），同一时刻最多一项 `in_progress`。清单跟随会话持久化（checkpoint +
sidecar `meta.json`），`/rewind` 回溯时会恢复到当时的状态。清单本身不改文件系统，任何权限模式下都免审批，
用户不能手动触发 `todo_write`，但可以用 `/todos` 随时查看，或 `/todos clear` 手动清空。

## 工具

| 工具 | 类型 | 说明 |
| --- | --- | --- |
| `list_dir` | 只读 | 树形列出目录，自动跳过 `.git`、`node_modules` 等 |
| `read_file` | 只读 | 带行号读取，支持 offset/limit 分段 |
| `glob` | 只读 | 按文件名 glob 查找，支持 `**` 与一层花括号，按 mtime 最多 100 条 |
| `grep` | 只读 | 基于 ripgrep 搜内容；默认只返回路径，遵守 `.gitignore` |
| `memory_list` | 只读 | 列出 `.cyan/memory/` 中的记忆文件 |
| `memory_read` | 只读 | 读取某一个记忆 md |
| `memory_write` | 写入 | 写入四类笔记之一并更新索引；非 Plan 下免审批 |
| `write_file` | 写入 | 整文件写入，自动创建父目录 |
| `edit_file` | 写入 | 精确字符串替换，要求匹配唯一 |
| `todo_write` | 写入 | 整体覆盖式更新任务规划清单；不改文件系统，任何模式下免审批 |
| `bash` | 执行 | 唯一的 shell 执行入口：测试、构建、git、脚本都走它 |

`bash` 每次调用都是独立新进程，不保留环境变量或别名，`export` 不会带到下一次调用；
但工作目录会在调用之间延续——命令里 `cd` 到哪，下一次调用就从哪继续，越出工作目录会被自动拉回工作目录根。
system prompt 里会给出本机 Python 解释器的绝对路径，避免模型在命令里写出环境中并不存在的 `python`。

## 安全模型

判定顺序：工作区沙箱 → `deny` → 关键删除询问 → `ask` → 只读 bash / allow → 三种模式与会话白名单。

1. **工作区沙箱**：路径必须落在工作区内。关键 `rm` / `rmdir`（`/`、顶级目录、家目录、工作区或其父目录，含 `$VAR/*` 与命令替换）不当成区外拒绝，改为强制询问：`allow` 不能预先批准，但可以点 `y`。
2. **声明式规则**（`allow` / `ask` / `deny`）：内置 [`defaults.json`](src/cyan/security/defaults.json) + `~/.cyan/settings.json` + 项目 `.cyan/settings.json` + `.cyan/settings.local.json`。写法：`Bash(pytest *)` / `Read(.env)` / `Edit(src/**)` / `WebFetch(domain:example.com)`。`Tool(param:value)` 按顶级输入参数匹配（仅 deny/ask，例如 `Bash(timeout_ms:1)`）；主要内容字段（`command` / `path` / `url`）不能这么写。`Write` 裸名仍匹配写入工具；`Write(路径)` 会收下但不做路径检查，请用 `Edit`。deny 压过 allow；`ask` 强制确认，没有「始终允许」。`sudo` 是内置 deny；`.env`、私钥、写 `.git` / `.vscode` / `.cyan` 是内置 ask。路径指定符支持 `src/**`（相对工作区）、`/src/**`（相对设置源）、`~/…`、`//绝对路径`。
3. **模式**：规则没覆盖时，只读 bash 所有模式免审批；Plan 拒写、AcceptEdits 放行普通写以及工作区内文件系统命令（受保护路径仍要确认）、Default 写入与非只读执行需确认。`y` 本次 / `n` 拒绝 / `a` 始终允许（写文件只活会话；bash 按子命令写入 local，最多 5 条）。`python -c` 这类看不清目标的命令走普通执行审批，`allow` 可以放行。`deny Read(.env)` 会连带挡住写入同一路径。

`/permissions` 列出规则；`allow|ask|deny` 写入 local；`remove` 可删 local / 项目 / 用户，不能删内置。写操作在确认前会展示完整 diff。

## 架构

分层解耦，CLI 与内核之间只通过事件流通信，内核不做任何输入输出：

```
cli/        REPL 与 rich 渲染，消费事件流、处理审批交互
core/       Agent Loop（Runtime + AgentLoop）、事件定义、identity system prompt
session/    会话状态、工具执行历史、工作区视图
context/    把消息历史与工具结果装配成发给模型的格式（叠 Prompt Layer）
prompt/     Prompt Layer：identity + cyan.md + MEMORY.md 索引
memory/     项目级 Auto Memory 存储与任务结束提取
llm/        模型客户端抽象与 DeepSeek 实现、输出解析
tools/      工具契约、注册表、文件系统与 bash 执行工具
security/   路径沙箱、硬地板、声明式规则、权限管理与审批协议
settings/   按域拆分的运行时设置（CLI 参数 > 环境变量 > 默认值）
logutil.py  标准库 logging（默认只写文件）
errors.py   异常体系
```

Agent Loop 是一个 generator：向外 yield 事件，通过 `send()` 接收审批决策。
工具的可预期失败不会中断循环，而是转成结构化结果回喂模型，由模型自行恢复。

终止条件：模型给出无工具调用的回复、达到轮次上限、连续工具失败、重复无效调用、用户中断。

完整设计与后续排期见 [docs/architecture.md](docs/architecture.md)。

## 扩展

新增工具：继承 `Tool`，填 `name`/`description`/`capability`/`parameters`，实现 `run()`，
然后在 `tools/registry.py` 的 `build_default_registry()` 里注册一行。JSON Schema 会自动导出给模型。

## 开发

```bash
uv sync --group dev
uv run pytest
```

推送到 `main` 或开 pull request 时，GitHub Actions 会按 `uv.lock` 安装依赖并跑同一套测试。

## 开发状态

Phase 1（最小可用闭环）已完成。会话事件日志、compact overlay、`--continue` / `--resume` 与 rewind fork、cyan.md Prompt Layer、项目级 Auto Memory、流式输出、丰富斜杠命令、任务规划工具 `todo_write`、斜杠命令实时下拉补全与等待动画、任务收尾摘要卡片（含总用时）、`read_file` 结果的语法高亮预览、`/changes` 改动文件清单均已落地。
