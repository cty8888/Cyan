# Coding Agent

一个命令行编程智能体：接到自然语言任务后，自主读取目录、读写文件、执行命令，直到任务完成。

不依赖任何 Agent 框架或 SDK（LangChain / LlamaIndex / OpenAI Agents SDK 等一概未用），
Agent Loop、工具系统、模型输出解析、安全策略、终止条件与错误恢复全部自行实现，
仅使用模型厂商的 OpenAI 兼容 API 与原生 Tool Calling 接口。

## 快速开始

```bash
uv sync
echo "DEEPSEEK_API_KEY=sk-..." > .env

# 交互模式
uv run coding-agent

# 单任务模式
uv run coding-agent -p "给 utils.py 加上类型标注并跑一遍测试"
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-p, --prompt` | 执行单个任务后退出 |
| `-w, --workspace` | 工作目录，默认当前目录；Agent 只能访问该目录内的文件 |
| `-m, --model` | 模型名称，默认 `deepseek-chat` |
| `--yolo` | 跳过写入与执行的逐次确认 |
| `--max-iterations` | 单任务最大轮次，默认 30 |
| `--verbose` | 额外把日志打到 stderr（默认只写文件，不干扰 rich 界面） |

终端界面用 rich 渲染（Markdown、diff、审批面板）。完整运行记录同时写入工作目录的 `.coding_agent/logs/agent.log`。

交互模式下可用 `/help`、`/tools`、`/usage`、`/clear`、`/cwd`、`/exit`，任务执行中按 Ctrl-C 中断。

## 工具

| 工具 | 风险等级 | 说明 |
| --- | --- | --- |
| `list_dir` | 只读 | 树形列出目录，自动跳过 `.git`、`node_modules` 等 |
| `read_file` | 只读 | 带行号读取，支持 offset/limit 分段 |
| `write_file` | 写入 | 整文件写入，自动创建父目录 |
| `edit_file` | 写入 | 精确字符串替换，要求匹配唯一 |
| `bash` | 执行 | 唯一的 shell 执行入口：测试、构建、git、脚本都走它 |

`bash` 每次调用都是独立新进程，不保留环境变量或别名，`export` 不会带到下一次调用；
但工作目录会在调用之间延续——命令里 `cd` 到哪，下一次调用就从哪继续，越出工作目录会被自动拉回工作目录根。
system prompt 里会给出本机 Python 解释器的绝对路径，避免模型在命令里写出环境中并不存在的 `python`。

## 安全模型

三道防线，从外到内依次生效：

1. **沙箱**：所有路径 `resolve()` 后必须落在工作目录内，`..` 与符号链接逃逸都会被拒绝。
2. **黑名单**：`rm -rf /`、`sudo`、`mkfs`、`curl | sh` 等致命命令直接拒绝，`--yolo` 也无法绕过。
3. **分级审批**：只读操作自动放行；写入与执行需确认，可选 `y` 允许 / `n` 拒绝 / `a` 本会话始终允许。
   `.env`、`.git/`、私钥等敏感文件的写入强制逐次确认，不受 `--yolo` 与「始终允许」影响。

写操作在确认前会展示完整 diff。

## 架构

分层解耦，CLI 与内核之间只通过事件流通信，内核不做任何输入输出：

```
cli/        REPL 与 rich 渲染，消费事件流、处理审批交互
core/       Agent Loop、会话状态、事件定义、system prompt
llm/        模型客户端抽象与 DeepSeek 实现、输出解析
tools/      工具契约、注册表、文件系统与 bash 执行工具
security/   路径沙箱、命令黑名单、风险分级与审批协议
config.py   三级配置覆盖（CLI 参数 > 环境变量 > 默认值）
logutil.py  标准库 logging（默认只写文件）
errors.py   异常体系
```

Agent Loop 是一个 generator：向外 yield 事件，通过 `send()` 接收审批决策。
工具的可预期失败不会中断循环，而是转成结构化结果回喂模型，由模型自行恢复。

终止条件：模型给出无工具调用的回复、达到轮次上限、连续工具失败、重复无效调用、用户中断。

完整设计与后续排期见 [docs/architecture.md](docs/architecture.md)。

## 扩展

新增工具：继承 `Tool`，填 `name`/`description`/`risk`/`parameters`，实现 `run()`，
然后在 `tools/registry.py` 的 `build_default_registry()` 里注册一行。JSON Schema 会自动导出给模型。

## 开发

```bash
uv run python tests/smoke.py   # 离线冒烟测试，用假 LLM 驱动完整循环，不访问网络
```

## 开发状态

Phase 1（最小可用闭环）已完成。后续：流式输出与富渲染打磨、上下文压缩与 Memory、
任务规划与搜索工具、pytest 测试。
